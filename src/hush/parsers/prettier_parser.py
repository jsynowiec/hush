import re

from hush.extract import Failure, format_location
from hush.parsers.base import Outcome

_UNFORMATTED = re.compile(r"^\[warn\] (?P<path>\S+)$")
_ERROR = re.compile(r"^\[error\] (?P<path>\S+): (?P<msg>.+?)(?: \((?P<line>\d+):\d+\))?$")


def parse(text: str) -> Outcome:
    failures: list[Failure] = []
    for line in text.splitlines():
        if m := _UNFORMATTED.match(line):
            failures.append(Failure(m["path"], ("File is not formatted",)))
        elif m := _ERROR.match(line):
            failures.append(Failure(format_location(m["path"], m["line"]), (m["msg"],)))
    return Outcome(failures=tuple(failures))
