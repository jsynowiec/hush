import re

from hush.extract import Failure
from hush.parsers.base import Outcome

_ERROR = re.compile(r"^\[(?P<code>[A-Z_]+)\] (?P<msg>.*)$")
_LOCATION = re.compile(r"^\s*╭─\[ (?P<path>.+?):(?P<line>\d+):\d+ \]$")


def parse(text: str) -> Outcome:
    entries: list[tuple[list[str], str | None]] = []
    for line in text.splitlines():
        if m := _ERROR.match(line):
            entries.append(([f"{m['code']}: {m['msg']}"], None))
        elif entries and entries[-1][1] is None and (m := _LOCATION.match(line)):
            entries[-1] = (entries[-1][0], f"{m['path']}:{m['line']}")
    return Outcome(failures=tuple(Failure(loc, tuple(reason)) for reason, loc in entries))
