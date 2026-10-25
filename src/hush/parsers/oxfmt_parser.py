import re

from hush.extract import Failure, format_location
from hush.parsers.base import Outcome

# `src/a.js (2ms)` — a file `--check` lists as needing formatting.
_UNFORMATTED = re.compile(r"^(?P<path>\S+) \(\d+ms\)$")
# `,-[src/broken.js:1:11]` — the source span of a parse error, in the line below its header.
# The lazy match keeps `]` in the name intact, as in `,-[src/[id]/page.js:1:11]`.
_SPAN = re.compile(r",-\[(?P<path>.+?):(?P<line>\d+):\d+\]")
# The header line above a span: `x Unexpected token`.
_HEADER = re.compile(r"^x\s+(?P<message>.+)$")


def parse(text: str) -> Outcome:
    lines = text.splitlines()
    failures: list[Failure] = []
    for index, line in enumerate(lines):
        if m := _UNFORMATTED.match(line):
            failures.append(Failure(m["path"], ("File is not formatted",)))
            continue
        if m := _SPAN.search(line):
            header = _HEADER.match(lines[index - 1].strip()) if index else None
            message = header["message"] if header else "Parse error"
            failures.append(Failure(format_location(m["path"], m["line"]), (message,)))
    return Outcome(failures=tuple(failures))
