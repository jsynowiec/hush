"""Parse failures with tool-specific parsers and generic extraction.

Keep only parser failures that name existing files. If none remain, try the other parsers.
If no parser finds an existing file, use the generic extractor, which falls back to the output tail.
The extra parser attempts also handle commands such as `pnpm run <script>` and `make`.
"""

import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace

from hush import extract
from hush.extract import Failure, PathExists
from hush.parsers import (
    biome_parser,
    diagnostics_parser,
    eslint_parser,
    mypy_parser,
    oxfmt_parser,
    oxlint_parser,
    prettier_parser,
    pytest_parser,
    tsc_parser,
    vite_parser,
    vitest_parser,
)
from hush.parsers.base import Outcome, Parser

PARSERS: Mapping[str, Parser] = {
    "pytest": pytest_parser.parse,
    "vitest": vitest_parser.parse,
    "ruff": diagnostics_parser.parse,
    "ty": diagnostics_parser.parse,
    "mypy": mypy_parser.parse,
    "tsc": tsc_parser.parse,
    "biome": biome_parser.parse,
    "vite": vite_parser.parse,
    "eslint": eslint_parser.parse,
    "oxlint": oxlint_parser.parse,
    "oxfmt": oxfmt_parser.parse,
    "prettier": prettier_parser.parse,
}

# `(?<!\d)` keeps the scan linear on long digit runs.
_PASSED = re.compile(r"(?<!\d)(\d+) passed\b")
_LINE_SUFFIX = re.compile(r":\d+$")


@dataclass(frozen=True, slots=True)
class Diagnosis:
    failures: tuple[Failure, ...]
    # Failures a tool parser found whose file could not be located. They are counted, not shown.
    unlocated: int = 0


def parse(tool: str | None, text: str, path_exists: PathExists, detail_limit: int | None = None) -> Diagnosis:
    for parser in _parsers_to_try(tool):
        found = parser(text).failures
        valid = tuple(f for f in found if _names_existing_file(f, path_exists))
        if valid:
            return Diagnosis(valid, unlocated=len(found) - len(valid))
    return Diagnosis(extract.extract_failures(text, path_exists, detail_limit))


def passed_summary(tool: str | None, text: str) -> Outcome:
    """The counts for the pass line: from the tool's parser, else the last `N passed` in the output."""
    parser = PARSERS.get(tool or "")
    specific = parser(text) if parser else Outcome()
    if replace(specific, failures=()) != Outcome():
        return specific
    matches = _PASSED.findall(text)
    return Outcome(test_count=int(matches[-1])) if matches else Outcome()


def passed_count(tool: str | None, text: str) -> int | None:
    return passed_summary(tool, text).test_count


def _parsers_to_try(tool: str | None) -> Iterator[Parser]:
    first = PARSERS.get(tool or "")
    if first:
        yield first
    yield from (p for p in dict.fromkeys(PARSERS.values()) if p is not first)


def _names_existing_file(failure: Failure, path_exists: PathExists) -> bool:
    """The whole path part must exist, so text that merely starts with a real path is rejected."""
    if failure.location is None:
        return False
    path = failure.location.split("::")[0]
    return path_exists(_LINE_SUFFIX.sub("", path))
