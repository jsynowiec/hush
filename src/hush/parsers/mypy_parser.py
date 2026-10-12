import re

from hush.extract import Failure
from hush.parsers.base import Outcome

_DIAGNOSTIC = re.compile(r"^(?P<path>[^\s:][^:]*):(?P<line>\d+)(?::\d+)?: (?P<severity>error|note): (?P<msg>.*)$")


def parse(text: str) -> Outcome:
    entries: list[tuple[str, list[str]]] = []
    for line in text.splitlines():
        m = _DIAGNOSTIC.match(line)
        if not m:
            continue
        if m["severity"] == "error":
            entries.append((f"{m['path']}:{m['line']}", [m["msg"]]))
        elif entries:
            entries[-1][1].append(m["msg"])
    return Outcome(failures=tuple(Failure(loc, tuple(reason)) for loc, reason in entries))
