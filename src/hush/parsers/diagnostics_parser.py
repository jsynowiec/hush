import re

from hush.extract import Failure
from hush.parsers.base import Outcome

# ruff and ty print a header line, then `--> path:line:col`.
_ARROW = re.compile(r"^\s*--> (?P<path>.+?):(?P<line>\d+):\d+$")


def parse(text: str) -> Outcome:
    lines = text.splitlines()
    failures = tuple(
        Failure(location=f"{m['path']}:{m['line']}", reason=(lines[i - 1].strip(),))
        for i, line in enumerate(lines)
        if i > 0 and (m := _ARROW.match(line))
    )
    return Outcome(failures=failures)
