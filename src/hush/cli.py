import argparse
import contextlib
import functools
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
# Pruning deletes only names that match `<slug>-<YYYYMMDD>T<HHMMSS>-<4 hex>.log`.
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
        description="Run a command with one line on success and a short report on failure.",
    )
    parser.add_argument("--name", default="", help='step name for the report and log, for example "Lint check"')
    parser.add_argument("--tool", help="override the tool name used for the report label and output parser")
    parser.add_argument(
        "--cwd",
        type=Path,
        help="run COMMAND in this directory, with reported paths relative to where hush started",
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
        help="stop the local process group after this many seconds (container processes can continue)",
    )
    parser.add_argument(
        "--log-dir", default=DEFAULT_LOG_DIR, help="directory for cleaned, redacted logs, subject to capture limits"
    )
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
    parser.add_argument(
        "--verbose", action="store_true", help="print cleaned, redacted output to stderr after the command exits"
    )
    parser.add_argument(
        "--classify",
        action="store_true",
        help="classify COMMAND without running it: print a recognised tool and exit 0, otherwise exit 1",
    )
    parser.add_argument("command", nargs="+", help="command to run (put it after `--`)")
    return parser


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "run"


def _run_id() -> str:
    """Return the UTC time and four random hex digits to reduce log-name collisions."""
    return f"{datetime.now(UTC):%Y%m%dT%H%M%S}-{secrets.token_hex(2)}"


def _log_name(prefix: str, tool: str, name: str, run_id: str) -> str:
    slug = _slug(f"{prefix} {tool} {name}")
    if len(slug) > MAX_LOG_NAME_CHARS:
        digest = hashlib.sha256(slug.encode()).hexdigest()[:8]
        slug = f"{slug[: MAX_LOG_NAME_CHARS - 9].rstrip('-')}-{digest}"
    return f"{slug}-{run_id}.log"


def _child_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Inherit the environment and disable tool colour output."""
    return {**environ, "NO_COLOR": "1", "FORCE_COLOR": "0"}


def _relativize(text: str, directory: Path) -> str:
    """Remove the working-directory prefix from absolute paths.

    Only whole absolute prefixes are removed, so a directory name that happens to appear
    elsewhere in a path is left alone.
    """
    prefixes = sorted({str(directory.absolute()), str(directory.resolve())}, key=len, reverse=True)
    for prefix in prefixes:
        text = re.sub(rf"(?<![\w./-]){re.escape(prefix)}/", "", text)
    return text


def _host_workdir(container_dir: str | None, base: Path) -> tuple[str, str]:
    """Return a guessed host directory under `base` and its container mount root.

    Choose the longest suffix of the container working directory that exists under `base`.
    For `/workspace/backend`, try `workspace/backend`, then `backend`. Matching `backend`
    gives the guessed mount root `/workspace`. Both results are empty when no directory matches.
    Reported files must exist on the host even when a directory matches.
    """
    if not container_dir:
        return "", ""
    root = Path.cwd().resolve()
    parts = [part for part in container_dir.split("/") if part]
    for start in range(len(parts)):
        tail = "/".join(parts[start:])
        if _is_inside(base / tail, root, Path.is_dir):
            return tail, ("/" + "/".join(parts[:start]) if start else "")
    return "", ""


_PATH = r"[\w@+.-]+(?:/[\w@+.-]+)*"


def _rewrite_under(text: str, prefix: str, target: Callable[[str], str | None]) -> str:
    """Replace each absolute path `prefix/rest` with `target(rest)`, or leave it as printed when that is None.

    A trailing full stop remains outside `rest` so that the path lookup excludes sentence punctuation.
    """
    if f"{prefix}/" not in text:
        return text

    @functools.cache
    def mapped(rest: str) -> str | None:
        return target(rest)

    def rewrite(match: re.Match[str]) -> str:
        rest = match["rest"].rstrip(".")
        new = mapped(rest) if rest else None
        return match[0] if new is None else new + match["rest"][len(rest) :]

    return re.sub(rf"(?<![\w./-]){re.escape(prefix)}/(?P<rest>{_PATH})", rewrite, text)


def _strip_workdir(text: str, container_dir: str | None, locate: Callable[[str], str | None]) -> str:
    """Make absolute paths under the container working directory relative to it, so they can be matched to host files.

    Only paths that are files on the host change, so library paths such as `/usr/local/lib/...` stay as printed.
    """
    if not container_dir or container_dir == "/":
        return text
    return _rewrite_under(text, container_dir, lambda rest: rest if locate(rest) is not None else None)


def _map_mount_root(text: str, mount_root: str, workdir: str, exists: Callable[[str], bool]) -> str:
    """Point absolute container paths in the mounted tree, outside the working directory, at the host.

    `mount_root` is the container folder that holds the working directory's host folder `workdir`.
    `/workspace/shared/x.py` becomes `../shared/x.py`, relative to the working directory, but only
    when that file exists on the host, so other text is left as it was.
    """
    up = "../" * (workdir.count("/") + 1)
    return _rewrite_under(text, mount_root, lambda rest: up + rest if exists(up + rest) else None)


def _is_inside(path: Path, root: Path, kind: Callable[[Path], bool]) -> bool:
    """Whether `path`, after resolving `..` and symlinks, is under `root` and passes `kind` (`Path.is_file`)."""
    # Python 3.12 and 3.13 raise on over-long names, unreadable directories and symlink loops.
    try:
        resolved = path.resolve()
        return resolved.is_relative_to(root) and kind(resolved)
    except (OSError, RuntimeError, ValueError):
        return False


def _workdir_confirmed(text: str, workdir: str, locate: Callable[[str], str | None]) -> bool:
    """Whether a file in the output was found under the guessed `workdir` folder.

    A matching file permits use of the guessed mount root. A directory-name match alone does not
    confirm where the rest of the container tree maps on the host.
    """
    failures = extract.extract_failures(text, lambda path: locate(path) is not None, detail_limit=0)
    return any(f.location and locate(f.location.partition(":")[0]) == workdir for f in failures)


def _exists_in(directory: Path) -> Callable[[str], bool]:
    """Whether a reported path is a file under `directory` and inside where hush started.

    The second condition is checked after resolving `..` and symlinks, so a tool cannot point the
    report at files outside the repository. It is not tied to `--cwd`, so `../module-b/x.ts`
    still works from `--cwd module-a`.
    """
    root = Path.cwd().resolve()

    def exists(path: str) -> bool:
        return not os.path.isabs(path) and _is_inside(directory / path, root, Path.is_file)

    return exists


def _locator(directory: Path, workdir: str) -> Callable[[str], str | None]:
    """Return the matching base for a reported path: `workdir`, an empty string, or None.

    Try the guessed container directory first, then `directory` itself. A directory can match
    `workdir` by chance, as when a project with an `app/` package mounts the whole project at `/app`.
    """
    bases = [workdir, ""] if workdir else [""]
    lookups = [(base, _exists_in(directory / base)) for base in bases]

    @functools.cache
    def locate(path: str) -> str | None:
        return next((base for base, exists in lookups if exists(path)), None)

    return locate


def _prefixed(failure: extract.Failure, start: str, locate: Callable[[str], str | None]) -> extract.Failure:
    """Make the location relative to where hush started: `start` is the `--cwd` folder, or ""."""
    if failure.location is None:
        return failure
    path, colon, rest = failure.location.partition(":")
    prefix = posixpath.join(start, locate(path) or "")
    if not prefix:
        return failure
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
        # Sort the current log first so that pruning keeps it even when timestamps are equal.
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
        # Classification uses only the command. It ignores `--tool` and `--cwd`.
        name, index = tools.find_tool(args.command)
        if index is None:
            return 1
        print(name)
        return 0
    if args.cwd and not args.cwd.is_dir():
        parser.error(f"--cwd {args.cwd}: not a directory")
    child_cwd: Path = args.cwd or Path.cwd()
    container_dir = tools.exec_workdir(args.command)
    workdir, mount_root = _host_workdir(container_dir, child_cwd)
    locate = _locator(child_cwd, workdir)
    start = posixpath.normpath(args.cwd.as_posix()) if args.cwd else ""

    inferred_tool, tool_index = tools.find_tool(args.command)
    tool = args.tool or inferred_tool
    command = tools.with_failfast(args.command, inferred_tool, tool_index) if args.failfast else list(args.command)
    label = f"({tool}) {args.name}".rstrip()
    log_path = Path(args.log_dir) / _log_name(posixpath.join(start, workdir), tool, args.name, _run_id())
    # Apply environment-secret rules to values passed into the container.
    redactor = sanitize.Redactor(os.environ, tools.exec_env(command))

    result = runner.run_command(
        command,
        cwd=child_cwd,
        env=_child_env(os.environ),
        timeout=args.timeout,
        max_capture_bytes=args.max_capture_mb * 1024 * 1024,
    )
    # Redact multiline secrets before parsers split the output into lines.
    text = _relativize(
        redactor.redact_multiline(sanitize.normalize(result.output.decode("utf-8", errors="replace"), redactor.redact)),
        child_cwd,
    )
    text = _strip_workdir(text, container_dir, locate)
    if result.exit_code != 0 and mount_root and f"{mount_root}/" in text and _workdir_confirmed(text, workdir, locate):
        text = _map_mount_root(text, mount_root, workdir, _exists_in(child_cwd / workdir))
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
        else parsers.parse(tool, text, lambda path: locate(path) is not None, args.max_failures)
    )
    failures = tuple(_prefixed(_redacted(f, redactor), start, locate) for f in diagnosis.failures)
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
    """Write the log and return an error note if the write fails."""
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
