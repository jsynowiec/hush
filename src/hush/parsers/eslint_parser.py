import re

from hush.extract import Failure
from hush.parsers.base import Outcome

# Default "stylish" format: a file path line, then indented `line:col  severity  message  rule`.
_FILE = re.compile(r"^(?P<path>\S.*)$")
_PROBLEM = re.compile(r"^\s+(?P<line>\d+):\d+\s+(?P<severity>error|warning)\s+(?P<body>.*)$")
_SUMMARY = re.compile(r"^✖ \d+ problems?")


def parse(text: str) -> Outcome:
    failures: list[Failure] = []
    path: str | None = None
    for line in text.splitlines():
        if m := _PROBLEM.match(line):
            if path and m["severity"] == "error":
                failures.append(Failure(f"{path}:{m['line']}", (_reason(m["body"]),)))
        elif (m := _FILE.match(line)) and not _SUMMARY.match(line):
            path = m["path"]
    return Outcome(failures=tuple(failures))


def _reason(body: str) -> str:
    """`message  rule`: eslint pads the rule name with two or more spaces."""
    message, separator, rule = body.rstrip().rpartition("  ")
    return f"{rule.strip()}: {message.strip()}" if separator and message.strip() else body.strip()
