import re

from hush.extract import Failure
from hush.runner import RunResult

MAX_REASON_LINES = 8
MAX_LINE_CHARS = 240
_INDENT = "    "
_GREEN = "\x1b[32m"
_RED = "\x1b[31m"
_RESET = "\x1b[0m"


def success_line(label: str, test_count: int | None, seconds: float, *, color: bool = False) -> str:
    noun = "test" if test_count == 1 else "tests"
    count = f" ({test_count} {noun})" if test_count is not None else ""
    return f"  {_mark('✔ PASS', _GREEN, color)} {label} passed{count}; {seconds:.1f}s"


def failure_report(
    label: str,
    result: RunResult,
    failures: tuple[Failure, ...],
    log_path: str,
    max_failures: int,
    unlocated: int = 0,
    *,
    color: bool = False,
) -> str:
    parts = [f"  {_mark('✗ FAIL', _RED, color)} {label} {_verdict(result)}; {result.seconds:.1f}s"]
    if result.spawn_error:
        parts.append(f"\n{_INDENT}{result.spawn_error}")
    elif not failures:
        parts.append(f"\n{_INDENT}The command printed nothing (exit code {result.exit_code}).")
    for failure in failures[:max_failures]:
        parts.append("\n" + _failure_block(failure))
    hidden = len(failures) - max_failures
    if hidden > 0:
        parts.append(f"\n{_INDENT}... and {hidden} more (see log)")
    if unlocated:
        parts.append(f"\n{_INDENT}... and {unlocated} more not matched to a file (see log)")
    if result.omitted_bytes:
        parts.append(f"\n{_INDENT}The output was very long. The log omits {result.omitted_bytes} bytes.")
    parts.append(f"\n{_INDENT}Details: {log_path}")
    return "\n".join(parts)


def _mark(text: str, code: str, color: bool) -> str:
    return f"{code}{text}{_RESET}" if color else text


def _verdict(result: RunResult) -> str:
    if result.timed_out:
        return "timed out"
    if result.interrupted_by is not None:
        return "interrupted"
    return "failed"


def _failure_block(failure: Failure) -> str:
    lines = [f"{_INDENT}{_clip(failure.location)}"] if failure.location else []
    if failure.reason:
        lines.append(f"{_INDENT}Reason:")
        shown = failure.reason[:MAX_REASON_LINES]
        lines.extend(f"{_INDENT}  {_clip(line)}" for line in shown)
        if len(failure.reason) > len(shown):
            lines.append(f"{_INDENT}  ... ({len(failure.reason) - len(shown)} more lines in log)")
    return "\n".join(lines)


def _clip(line: str) -> str:
    line = re.sub(r"\s{2,}", " ", line)
    return line if len(line) <= MAX_LINE_CHARS else line[: MAX_LINE_CHARS - 1] + "…"
