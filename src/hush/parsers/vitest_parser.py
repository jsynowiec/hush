import re

from hush.extract import Failure, format_location
from hush.parsers.base import Outcome

# A workspace project prefixes the name: ` FAIL  |unit| src/a.test.ts > t`.
_FAIL = re.compile(r"^ FAIL  (?:\|[^|]*\| )?(?P<name>.+)$")
# A file that fails to load is titled `src/a.test.ts [ src/a.test.ts ]`.
_FILE_SUFFIX = re.compile(r"^(\S+) \[ .+ \]$")
# A frame is `❯ src/a.ts:4:17`, or `❯ fn src/a.ts:4:17` when the function has a name.
_FRAME = re.compile(r"^ ❯ (?:.* )?(?P<path>\S+):(?P<line>\d+):\d+$")
_TESTS_LINE = re.compile(r"^\s+Tests\s+(?P<body>.+)$")
_PASSED = re.compile(r"(?<!\d)(\d+) passed\b")


def parse(text: str) -> Outcome:
    lines = text.splitlines()
    return Outcome(failures=tuple(_failures(lines)), test_count=_passed_count(lines))


def _failures(lines: list[str]) -> list[Failure]:
    failures: list[Failure] = []
    block: list[str] | None = None
    name = ""
    for line in lines:
        if m := _FAIL.match(line):
            if block is not None:
                failures.append(_failure(name, block))
            name, block = m["name"], []
        elif block is not None and line.startswith("⎯"):
            failures.append(_failure(name, block))
            block = None
        elif block is not None:
            block.append(line)
    if block is not None:
        failures.append(_failure(name, block))
    return failures


def _failure(name: str, block: list[str]) -> Failure:
    name = _FILE_SUFFIX.sub(r"\1", name)
    test_file = name.split(" > ")[0]
    reason: list[str] = []
    line_number: str | None = None
    before_frames = True
    for line in block:
        if m := _FRAME.match(line):
            before_frames = False
            if m["path"] == test_file:
                line_number = m["line"]
                break
        elif before_frames and line.strip():
            reason.append(line.rstrip())
    return Failure(location=format_location("::".join(name.split(" > ")), line_number), reason=tuple(reason))


def _passed_count(lines: list[str]) -> int | None:
    for line in reversed(lines):
        if (m := _TESTS_LINE.match(line)) and (passed := _PASSED.search(m["body"])):
            return int(passed[1])
    return None
