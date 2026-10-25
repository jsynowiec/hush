import re

from hush.extract import Failure
from hush.parsers.base import Outcome

# `path:line:col: error rule(message): text help: ...`, one diagnostic per line.
# `--fix` prints the same lines for the problems it could not fix.
_DIAGNOSTIC = re.compile(r"^(?P<path>.+?):(?P<line>\d+):\d+: error (?P<rule>[\w-]+\([\w-]+\)): (?P<message>.*)$")
_HELP = " help: "


def parse(text: str) -> Outcome:
    failures = tuple(
        Failure(
            f"{m['path']}:{m['line']}",
            (f"{m['rule']}: {m['message'].partition(_HELP)[0].rstrip()}",),
        )
        for line in text.splitlines()
        if (m := _DIAGNOSTIC.match(line))
    )
    return Outcome(failures=failures)
