"""Tool-agnostic failure extraction.

Nothing here knows about a specific tool. It relies on conventions that survive tool
releases: editors need `path:line` references, so tools keep printing them; related lines
sit together in blank-line separated paragraphs; and sections are divided by runs of one
repeated symbol. Which symbol, colour or wording is used does not matter.

Every step is linear in the size of the output, and long lines are clipped before they get here.
"""

import bisect
import functools
import re
from array import array
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePath

TAIL_LINES = 20
SELF_CONTAINED_WORDS = 3
MIN_CONTEXT_LINES = 2
BACK_CONTEXT_LINES = 6
FORWARD_CONTEXT_LINES = 3

# `path:12`, `path:12:5`, `path(12,5)`. The path needs a file extension.
_LINE_REF = re.compile(
    r"(?<![\w/.@+-])(?P<path>(?:\.{1,2}/|/)?[\w@+.-]+(?:/[\w@+.-]+)*\.[A-Za-z]\w{0,7})"
    r"[:(](?P<line>\d+)(?:[:,]\d+)?\)?"
)
# A file path that starts a line, optionally after a `[label]` prefix: `[warn] src/a.ts`.
_BARE_REF = re.compile(
    r"^\s*(?:\[\w+\]\s+)?(?P<path>(?:\.{1,2}/)?[\w@+.-]+(?:/[\w@+.-]+)*\.[A-Za-z]\w{0,7})(?=[\s:]|$)"
)
# 4+ of the same symbol at the start of a line: `____ test ____`, `━━━━`, `==== x ====`.
_SEPARATOR = re.compile(r"^ ?(?P<symbol>[^A-Za-z0-9\s])(?P=symbol){3,}")
# Source-code gutter lines such as `12 | code`, `> 5 │ code`, `3 3 │ code`.
_GUTTER = re.compile(r"^\s*(?:[>!]\s*)?\d+(?:\s+\d+)?\s*[|│┃+]")
_EDGE_PUNCTUATION = " \t:;,>|│┃"
_TRAILING_RUN_MIN = 3
_LIBRARY_DIRS = frozenset(
    {
        "node_modules",
        "site-packages",
        "dist-packages",
        ".venv",
        "venv",
        ".pnpm",
        "__pypackages__",
    }
)

PathExists = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class Failure:
    """One thing that failed. `location` is `path`, `path:line` or `path::test:line`."""

    location: str | None
    reason: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Ref:
    index: int
    path: str
    line: str | None
    # The reference sits at `line[start:end]`. The text around it is only cut out for the
    # references that are shown, so a flood of references does not copy every line.
    start: int
    end: int


def format_location(path: str, line: str | None) -> str:
    return f"{path}:{line}" if line else path


def extract_failures(text: str, path_exists: PathExists, detail_limit: int | None = None) -> tuple[Failure, ...]:
    """Locations with a few lines of explanation, or the output tail when none are found.

    Only the first `detail_limit` failures get an explanation. The rest are listed by location.
    """
    lines = text.splitlines()
    refs = _find_refs(lines, functools.cache(path_exists))
    if not refs:
        return _tail(lines)
    layout = _Layout(lines)
    ref_lines = sorted({ref.index for ref in refs})
    failures = (
        Failure(
            format_location(ref.path, ref.line),
            _reason(ref, layout, ref_lines) if detail_limit is None or n < detail_limit else (),
        )
        for n, ref in enumerate(refs)
    )
    return tuple(dict.fromkeys(failures))


def _tail(lines: list[str]) -> tuple[Failure, ...]:
    kept: list[str] = []
    for line in reversed(lines):
        if line.strip():
            kept.append(line.rstrip())
            if len(kept) == TAIL_LINES:
                break
    return (Failure(location=None, reason=tuple(reversed(kept))),) if kept else ()


def _is_separator(line: str) -> bool:
    return bool(_SEPARATOR.match(line))


def _is_content(line: str) -> bool:
    """A line that says something: not blank, a separator, a source gutter or pure decoration."""
    return any(c.isalnum() for c in line) and not _is_separator(line) and not _GUTTER.match(line)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _tidy(text: str) -> str:
    text = text.rstrip()
    run = len(text) - len(text.rstrip(text[-1:]))
    if run >= _TRAILING_RUN_MIN and not text[-1].isalnum():
        text = text[:-run].rstrip()
    return text.strip(_EDGE_PUNCTUATION)


def _says_something(text: str) -> str:
    return text if any(c.isalnum() for c in text) else ""


def _is_library_path(path: str) -> bool:
    parts = PurePath(path)
    return parts.is_absolute() or bool(_LIBRARY_DIRS & set(parts.parts))


def _find_refs(lines: list[str], path_exists: PathExists) -> list[_Ref]:
    refs: list[_Ref] = []
    for index, line in enumerate(lines):
        for m in _LINE_REF.finditer(line):
            if _is_library_path(m["path"]) or not path_exists(m["path"]):
                continue
            refs.append(_Ref(index, m["path"], m["line"], m.start(), m.end()))
    located = {ref.path for ref in refs}
    for index, line in enumerate(lines):
        m = _BARE_REF.match(line)
        if m and m["path"] not in located and not _is_library_path(m["path"]) and path_exists(m["path"]):
            located.add(m["path"])
            refs.append(_Ref(index, m["path"], None, 0, m.end()))
    return sorted(refs, key=lambda ref: ref.index)


def _remainder(ref: _Ref, lines: list[str]) -> str:
    """What the line says apart from the reference itself."""
    line = lines[ref.index]
    return _says_something(_tidy(f"{line[: ref.start]} {line[ref.end :]}"))


class _Layout:
    """Paragraph bounds and next-non-blank positions, computed once so lookups are O(1).

    Kept in `array`s because a list costs about 36 bytes per line and number, and the output
    can have millions of lines.
    """

    def __init__(self, lines: list[str]) -> None:
        count = len(lines)
        self.lines = lines
        wall = bytearray(not line.strip() or _is_separator(line) for line in lines)
        self._start = array("i", range(count))
        self._end = array("i", range(1, count + 1))
        i = 0
        while i < count:
            if wall[i]:
                i += 1
                continue
            j = i
            while j < count and not wall[j]:
                j += 1
            self._start[i:j] = array("i", [i]) * (j - i)
            self._end[i:j] = array("i", [j]) * (j - i)
            i = j
        self._next_nonblank = array("i", [count]) * (count + 1)
        for k in range(count - 1, -1, -1):
            self._next_nonblank[k] = k if lines[k].strip() else self._next_nonblank[k + 1]

    def paragraph(self, index: int) -> tuple[int, int]:
        return self._start[index], self._end[index]

    def followed_by_wall(self, end: int) -> bool:
        """True when the next non-blank line is a separator, or the output ends."""
        nxt = self._next_nonblank[end]
        return nxt == len(self.lines) or _is_separator(self.lines[nxt])


def _reason(ref: _Ref, layout: _Layout, ref_lines: list[int]) -> tuple[str, ...]:
    lines = layout.lines
    remainder = _remainder(ref, lines)
    if len(remainder.split()) >= SELF_CONTAINED_WORDS:
        return (remainder,)
    start, end = layout.paragraph(ref.index)
    own = [remainder] if remainder else []
    where = bisect.bisect_left(ref_lines, ref.index)
    previous = ref_lines[where - 1] if where > 0 else -1
    following = ref_lines[where + 1] if where + 1 < len(ref_lines) else len(lines)
    if previous >= start or following < end:
        # Several references share this paragraph. Only indented lines right under the
        # reference are its own. Anything else could belong to a neighbour.
        return tuple([*own, *_continuation(lines, ref.index, min(following, end))])
    around = [line.strip() for i, line in enumerate(lines[start:end], start) if i != ref.index and _is_content(line)]
    context = [*own, *around] if ref.index == start else [*around, *own]
    if len(context) >= MIN_CONTEXT_LINES:
        return tuple(context)
    if layout.followed_by_wall(end):
        return (*_neighbours(layout, start, step=-1, budget=BACK_CONTEXT_LINES), *context)
    return (*context, *_neighbours(layout, end - 1, step=1, budget=FORWARD_CONTEXT_LINES))


def _continuation(lines: list[str], index: int, stop: int) -> list[str]:
    indent = _indent(lines[index])
    kept: list[str] = []
    for line in lines[index + 1 : stop]:
        if not _is_content(line):
            continue
        if _indent(line) <= indent:
            break
        kept.append(line.strip())
    return kept


def _neighbours(layout: _Layout, edge: int, step: int, budget: int) -> list[str]:
    """Content of the paragraphs next to `edge`, in reading order, until `budget` lines are found.

    Stops at a separator or at a paragraph that holds its own file reference. When the budget is
    exceeded, keeps the lines nearest to `edge`.
    """
    lines = layout.lines
    collected: list[list[str]] = []
    total = 0
    i = edge + step
    while 0 <= i < len(lines) and total < budget:
        if _is_separator(lines[i]):
            break
        if not lines[i].strip():
            i += step
            continue
        start, end = layout.paragraph(i)
        block = lines[start:end]
        if any(_LINE_REF.search(line) for line in block):
            break
        content = [line.strip() for line in block if _is_content(line)]
        if content:
            collected.append(content)
            total += len(content)
        i = start - 1 if step < 0 else end
    ordered = [line for block in (reversed(collected) if step < 0 else collected) for line in block]
    return ordered[-budget:] if step < 0 else ordered[:budget]
