import argparse
import contextlib
import hashlib
import os
import posixpath
import re
import secrets
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

from hush import extract, parsers, report, runner, sanitize, tools

DEFAULT_LOG_DIR = ".tmp/test-results"
DEFAULT_KEEP_LOGS = 40
DEFAULT_MAX_CAPTURE_MB = 10
DEFAULT_MAX_FAILURES = 10
MAX_LOG_NAME_CHARS = 100
# `<slug>-<YYYYMMDD>T<HHMMSS>-<4 hex>.log`. Pruning only ever touches names that match.
LOG_NAME = re.compile(r"[a-z0-9-]+-\d{8}T\d{6}-[0-9a-f]{4}\.log")


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return value


def _positive_float(text: str) -> float:
    value = float(text)
    if value <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return value


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hush",
        usage="hush [options] -- COMMAND [ARGS...]",
        description="Run a command. Print one line if it passes, file:line details if it fails.",
    )
    parser.add_argument("--name", default="", help='what is being run, e.g. "Lint check"')
    parser.add_argument("--tool", help="tool name for the label and output parser when it cannot be inferred")
    parser.add_argument(
        "--cwd",
        type=Path,
        help="run COMMAND in this directory and report its paths from here",
    )
    parser.add_argument(
        "--failfast",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="add the tool's stop-at-first-failure flag where it has one (pytest, vitest)",
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        help="stop the command and its child processes after this many seconds",
    )
    parser.add_argument("--log-dir", default=DEFAULT_LOG_DIR, help="where full logs are written")
    parser.add_argument(
        "--keep-logs",
        type=_positive_int,
        default=DEFAULT_KEEP_LOGS,
        help="after each run, keep this many of the newest logs in --log-dir and delete the rest",
    )
    parser.add_argument("--max-failures", type=_positive_int, default=DEFAULT_MAX_FAILURES)
    parser.add_argument("--max-capture-mb", type=_positive_int, default=DEFAULT_MAX_CAPTURE_MB)
    parser.add_argument(
        "--color",
        choices=("auto", "always", "never"),
        default="auto",
        help="colour the pass and fail marks (auto: only on a terminal, never with NO_COLOR set)",
    )
    parser.add_argument("--verbose", action="store_true", help="also print the cleaned output to stderr afterwards")
    parser.add_argument(
        "--classify",
        action="store_true",
        help="do not run COMMAND; print the tool hush recognises in it and exit 0, or print nothing and exit 1",
    )
    parser.add_argument("command", nargs="+", help="command to run (put it after `--`)")
    return parser


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


def _run_id() -> str:
    """UTC time to the second plus 4 random hex digits, so concurrent runs of one step do not share a log."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


def _log_name(prefix: str, tool: str, name: str, run_id: str) -> str:
    slug = _slug(f"{prefix} {tool} {name}")
    if len(slug) > MAX_LOG_NAME_CHARS:
        digest = hashlib.sha256(slug.encode()).hexdigest()[:8]
        slug = f"{slug[: MAX_LOG_NAME_CHARS - 9].rstrip('-')}-{digest}"
    return f"{slug}-{run_id}.log"


def _child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Inherit the environment, but make tools colourless."""
    return {**environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}


def _relativize(text: str, directory: Path) -> str:
    """Drop the working directory from absolute paths, to save tokens.

    Only whole absolute prefixes are removed, so a directory name that happens to appear
    elsewhere in a path is left alone.
    """
    prefixes = sorted({str(directory.absolute()), str(directory.resolve())}, key=len, reverse=True)
    for prefix in prefixes:
        text = re.sub(rf"(?<![\w./-]){re.escape(prefix)}/", "", text)
    return text


def _host_workdir(container_dir: str | None, base: Path) -> str:
    """The folder under `base` that a container working directory most likely mounts, or "" for none.

    Tools print paths relative to the working directory the command ran in, which for a container
    command is the directory the command line sets there. The mount is not known, so this takes the
    longest tail of the container path that is a folder under `base`: `/workspace/backend` gives
    `workspace/backend` if that exists, else `backend`. It is a guess, but a reported file must
    still exist there to be named.
    """
    if not container_dir or not posixpath.isabs(container_dir):
        return ""
    parts = [part for part in posixpath.normpath(container_dir).split("/") if part]
    root = Path.cwd().resolve()
    for start in range(len(parts)):
        tail = "/".join(parts[start:])
        # Python 3.12 and 3.13 raise on over-long names, unreadable directories and symlink loops.
        try:
            candidate = (base / tail).resolve()
            if candidate.is_relative_to(root) and candidate.is_dir():
                return tail
        except (OSError, RuntimeError, ValueError):
            continue
    return ""


def _exists_in(directory: Path) -> Callable[[str], bool]:
    """Whether a reported path is a file under `directory` and inside where hush started.

    The second condition is checked after resolving `..` and symlinks, so a tool cannot point the
    report at files outside the repository. It is not tied to `--cwd`, so `../module-b/x.ts`
    still works from `--cwd module-a`.
    """
    root = Path.cwd().resolve()

    def exists(path: str) -> bool:
        if os.path.isabs(path):
            return False
        # Python 3.12 and 3.13 raise on over-long names, unreadable directories and symlink loops.
        try:
            candidate = (directory / path).resolve()
            return candidate.is_relative_to(root) and candidate.is_file()
        except (OSError, RuntimeError, ValueError):
            return False

    return exists


def _prefixed(failure: extract.Failure, prefix: str) -> extract.Failure:
    if not prefix or failure.location is None:
        return failure
    path, colon, rest = failure.location.partition(":")
    return extract.Failure(posixpath.normpath(f"{prefix}/{path}") + colon + rest, failure.reason)


def _redacted(failure: extract.Failure, redactor: sanitize.Redactor) -> extract.Failure:
    location = failure.location
    if location is not None:
        # The part after `::` is a test id, which can be any text. The file path stays readable.
        path, separator, test_id = location.partition("::")
        location = path + separator + redactor.redact(test_id)
    return extract.Failure(location, tuple(redactor.redact(line) for line in failure.reason))


def _write_log(path: Path, content: str) -> None:
    """Write the log through a private temporary file, replacing the target in one step.

    The replace swaps out a symlink at `path` instead of writing through it.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".hush-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(content)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise


def _prune_logs(current: Path, keep: int) -> None:
    """Delete all but the newest `keep` hush logs in the directory of `current`, which is always kept.

    Only names that match `LOG_NAME` are candidates. Failures are ignored: another run may be deleting
    the same files, and pruning must not change the outcome of this run.
    """
    with contextlib.suppress(OSError):
        logs: list[tuple[bool, int, str]] = []
        with os.scandir(current.parent) as entries:
            for entry in entries:
                if not LOG_NAME.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        mtime = entry.stat(follow_symlinks=False).st_mtime_ns
                        logs.append((entry.name == current.name, mtime, entry.name))
                except OSError:
                    continue
        # The run's own log sorts first whatever its timestamp, so equal timestamps cannot get it deleted.
        logs.sort(reverse=True)
        for _, _, name in logs[keep:]:
            with contextlib.suppress(OSError):
                os.unlink(current.parent / name)


def _display_path(path: Path) -> str:
    if not path.is_absolute():
        return path.as_posix()
    try:
        return str(path.relative_to(Path.cwd()))
    except ValueError:
        return str(path)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.classify:
        # Answers from the command alone, so `--tool` and `--cwd` play no part.
        name, index = tools.find_tool(args.command)
        if index is None:
            return 1
        print(name)
        return 0
    if args.cwd and not args.cwd.is_dir():
        parser.error(f"--cwd {args.cwd}: not a directory")
    child_cwd: Path = args.cwd or Path.cwd()
    workdir = _host_workdir(tools.exec_workdir(args.command), child_cwd)
    report_dir = child_cwd / workdir
    prefix = "/".join(part for part in (args.cwd.as_posix() if args.cwd else "", workdir) if part)
    prefix = posixpath.normpath(prefix) if prefix else ""

    inferred_tool, tool_index = tools.find_tool(args.command)
    tool = args.tool or inferred_tool
    command = tools.with_failfast(args.command, inferred_tool, tool_index) if args.failfast else list(args.command)
    label = f"({tool}) {args.name}".rstrip()
    log_path = Path(args.log_dir) / _log_name(prefix, tool, args.name, _run_id())
    # Values the command passes into a container are secrets the same way environment variables are.
    redactor = sanitize.Redactor(os.environ, tools.exec_env(command))

    result = runner.run_command(
        command,
        cwd=child_cwd,
        env=_child_env(os.environ),
        timeout=args.timeout,
        max_capture_bytes=args.max_capture_mb * 1024 * 1024,
    )
    # Secrets that span lines have to go before parsing, which looks at one line at a time.
    text = _relativize(
        redactor.redact_multiline(sanitize.normalize(result.output.decode("utf-8", errors="replace"), redactor.redact)),
        child_cwd,
    )
    log_note = _save_log(
        log_path,
        redactor.redact(
            _log_contents(tools.mask_exec_env(command, sanitize.is_secret_env_name, sanitize.REDACTED), text, result)
        ),
    )
    if log_note is None:
        _prune_logs(log_path, args.keep_logs)
    if args.verbose:
        print(redactor.redact(text), file=sys.stderr)

    color = args.color == "always" or (args.color == "auto" and sys.stdout.isatty() and "NO_COLOR" not in os.environ)
    if result.exit_code == 0:
        counts = parsers.passed_summary(tool, text)
        print(report.success_line(label, counts, result.seconds, color=color))
        return 0
    diagnosis = (
        parsers.Diagnosis(())
        if result.spawn_error
        else parsers.parse(tool, text, _exists_in(report_dir), args.max_failures)
    )
    failures = tuple(_prefixed(_redacted(f, redactor), prefix) for f in diagnosis.failures)
    print(
        report.failure_report(
            label,
            result,
            failures,
            log_note or _display_path(log_path),
            args.max_failures,
            diagnosis.unlocated,
            color=color,
        )
    )
    return result.exit_code


def _save_log(path: Path, content: str) -> str | None:
    """Write the log. Returns a note for the report when that was not possible."""
    try:
        _write_log(path, content)
    except OSError as error:
        return f"not written ({error.strerror})"
    return None


def _log_contents(command: Sequence[str], text: str, result: runner.RunResult) -> str:
    notes = []
    if result.omitted_bytes:
        notes.append(f"[hush] {result.omitted_bytes} bytes of output were left out")
    if result.timed_out:
        notes.append(f"[hush] stopped after the {result.seconds:.0f}s timeout")
    if result.interrupted_by is not None:
        notes.append(f"[hush] interrupted by signal {result.interrupted_by}")
    header = "$ " + " ".join(command)
    return "\n".join([header, f"exit code: {result.exit_code}", "", text.rstrip("\n"), *notes, ""])
