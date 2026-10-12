import re

from hush.extract import Failure, format_location
from hush.parsers.base import Outcome

# `path:line:col rule [FIXABLE] ━━━` for lint, `path rule ━━━` for format (no line number).
_HEADER = re.compile(r"^(?P<path>\S+?)(?::(?P<line>\d+):\d+)? (?P<rule>\S+)(?:\s+[A-Z]+)?\s+━+$")
_MESSAGE = re.compile(r"^\s+(?P<marker>[×!i]) (?P<text>.*)$")


def parse(text: str) -> Outcome:
    failures: list[Failure] = []
    header: re.Match[str] | None = None
    for line in text.splitlines():
        if m := _HEADER.match(line):
            header = m
        elif header and (msg := _MESSAGE.match(line)):
            if msg["marker"] == "×":
                location = format_location(header["path"], header["line"])
                reason = "File is not formatted" if header["rule"] == "format" else f"{header['rule']}: {msg['text']}"
                failures.append(Failure(location, (reason,)))
            header = None
    return Outcome(failures=tuple(failures))
