import re

from hush.extract import Failure, format_location
from hush.parsers.base import Outcome

_BLOCK_START = re.compile(r"^=+ (?:FAILURES|ERRORS) =+$")
_SUMMARY_START = re.compile(r"^=+ short test summary info =+$")
_ANY_HEADER = re.compile(r"^=+ .+ =+$")
# A title made only of `_` and spaces is pytest's `_ _ _ _` line between traceback frames.
_SECTION = re.compile(r"^_+ (?P<name>(?![_ ]+$).+?) _+$")
# `ERROR at setup of test_x` and `ERROR collecting tests/t.py` section titles.
_SECTION_PREFIX = re.compile(r"^ERROR (?:at (?:setup|teardown) of |collecting )")
_FRAME = re.compile(r"^(?P<path>\S+?\.py):(?P<line>\d+): ")
# Test ids may contain spaces, so read up to pytest's ` - message` separator.
_SUMMARY_ID = re.compile(r"^(?:FAILED|ERROR) (?P<nodeid>.+?)(?: - .*)?$")
_RESULT_LINE = re.compile(r"\bin \d+(?:\.\d+)?s\b")
_COUNT = re.compile(r"(?<!\d)(\d+) (passed|skipped|xfailed|xpassed|warning)s?\b")
_ERROR_LINE = re.compile(r"^E\s+(?P<text>.*)$")


def parse(text: str) -> Outcome:
    lines = text.splitlines()
    sections = _failure_sections(lines)
    nodeids = _nodeids_by_title_and_file(lines)
    failures = tuple(_failure(section, _section_key(title), nodeids) for title, section in sections)
    counts = _summary_counts(lines)
    return Outcome(
        failures=failures,
        test_count=counts.get("passed"),
        skipped=counts.get("skipped", 0),
        xfailed=counts.get("xfailed", 0),
        xpassed=counts.get("xpassed", 0),
        warnings=counts.get("warning", 0),
    )


def _failure_sections(lines: list[str]) -> list[tuple[str, list[str]]]:
    sections: list[tuple[str, list[str]]] = []
    in_block = False
    for line in lines:
        if _ANY_HEADER.match(line):
            in_block = bool(_BLOCK_START.match(line))
        elif in_block and (m := _SECTION.match(line)):
            sections.append((m["name"], []))
        elif in_block and sections:
            sections[-1][1].append(line)
    return sections


def _nodeids_by_title_and_file(lines: list[str]) -> dict[tuple[str, str], str]:
    """Test ids from the short summary, keyed by how a failure section titles them and by file.

    Sections and summary lines come in different orders, and two files can hold a test with the
    same name, so a section is matched by its title together with the file its own frames name.
    """
    by_title_and_file: dict[tuple[str, str], str] = {}
    in_summary = False
    for line in lines:
        if _SUMMARY_START.match(line):
            in_summary = True
        elif in_summary and _ANY_HEADER.match(line):
            break
        elif in_summary and (m := _SUMMARY_ID.match(line)):
            nodeid = m["nodeid"]
            by_title_and_file[(_nodeid_key(nodeid), nodeid.split("::")[0])] = nodeid
    return by_title_and_file


def _nodeid_key(nodeid: str) -> str:
    """`tests/t.py::Class::test_m` is titled `Class.test_m`. A bare path is titled as itself."""
    _, _, rest = nodeid.partition("::")
    return rest.replace("::", ".") if rest else nodeid


def _section_key(title: str) -> str:
    return _SECTION_PREFIX.sub("", title)


def _failure(section: list[str], key: str, nodeids: dict[tuple[str, str], str]) -> Failure:
    frames = [(m["path"], m["line"]) for line in section if (m := _FRAME.match(line))]
    reason = tuple(m["text"].rstrip() for line in section if (m := _ERROR_LINE.match(line)))
    return Failure(location=_location(_matching_nodeid(nodeids, key, frames), frames), reason=reason)


def _matching_nodeid(nodeids: dict[tuple[str, str], str], key: str, frames: list[tuple[str, str]]) -> str | None:
    """The id only when exactly one file in the section's frames has a summary line with this title.

    With none or several the section keeps its own frame location, not a guessed id.
    """
    candidates = {nodeids[k] for path, _ in frames if (k := (key, path)) in nodeids}
    return next(iter(candidates)) if len(candidates) == 1 else None


def _location(nodeid: str | None, frames: list[tuple[str, str]]) -> str | None:
    if nodeid is None:
        return format_location(*frames[-1]) if frames else None
    test_file = nodeid.split("::")[0]
    line = next((ln for path, ln in reversed(frames) if path == test_file), None)
    return format_location(nodeid, line)


def _summary_counts(lines: list[str]) -> dict[str, int]:
    """Counts from the last line that reads like pytest's `1 passed, 1 skipped in 0.05s` summary."""
    for line in reversed(lines):
        if _RESULT_LINE.search(line):
            counts: dict[str, int] = {}
            for m in _COUNT.finditer(line):
                counts.setdefault(m[2], int(m[1]))
            if counts:
                return counts
    return {}
