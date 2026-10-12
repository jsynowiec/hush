import re

from hush.extract import Failure
from hush.parsers.base import Outcome

_DIAGNOSTIC = re.compile(r"^(?P<path>.+?)\((?P<line>\d+),\d+\): error (?P<code>TS\d+): (?P<msg>.*)$")


def parse(text: str) -> Outcome:
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        if m := _DIAGNOSTIC.match(line):
            entries.append((f"{m['path']}:{m['line']}", [f"{m['code']}: {m['msg']}"]))
        elif entries and line.startswith((" ", "\t")) and line.strip():
            entries[-1][1].append(line.strip())
    return Outcome(failures=tuple(Failure(loc, tuple(reason)) for loc, reason in entries))
