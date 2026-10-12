import time
import tracemalloc

import pytest
from support import load_fixture as load

from hush import extract, sanitize


def exists_in(*paths: str):
    return lambda path: path in paths


def locations(name: str, *files: str) -> list[str | None]:
    failures = extract.extract_failures(load(name), exists_in(*files))
    return [f.location for f in failures]


def reasons(name: str, *files: str) -> list[str]:
    failures = extract.extract_failures(load(name), exists_in(*files))
    return [line for f in failures for line in f.reason]


@pytest.mark.parametrize(
    ("fixture", "files", "expected"),
    [
        (
            "pytest_fail",
            ["tests/test_a.py"],
            ["tests/test_a.py:6", "tests/test_a.py:8"],
        ),
        (
            "vitest_fail",
            ["src/math.test.ts"],
            ["src/math.test.ts:6", "src/math.test.ts:8"],
        ),
        ("tsc_fail", ["src/bad.ts", "src/math.ts"], ["src/bad.ts:1", "src/math.ts:4"]),
        ("mypy_fail", ["pkg/__init__.py"], ["pkg/__init__.py:5"]),
        ("ty_fail_default", ["pkg/__init__.py"], ["pkg/__init__.py:5"]),
        (
            "ruff_check_fail_full",
            ["pkg/__init__.py", "tests/test_a.py"],
            [
                "pkg/__init__.py:1",
                "pkg/__init__.py:1",
                "pkg/__init__.py:1",
                "tests/test_a.py:1",
            ],
        ),
        ("vite_fail", ["src/bad.ts"], ["src/bad.ts:1"]),
        (
            "biome_fail",
            ["src/math.ts", "src/math.test.ts"],
            ["src/math.ts:5", "src/math.test.ts:3"],
        ),
        (
            "pnpm_run_fail",
            ["src/bad.ts", "src/math.ts"],
            ["src/bad.ts:1", "src/math.ts:4"],
        ),
    ],
)
def test_locations_from_real_tool_output(fixture: str, files: list[str], expected: list[str]):
    assert locations(fixture, *files) == expected


def test_paths_that_do_not_exist_are_not_reported():
    assert locations("tsc_fail", "src/math.ts") == ["src/math.ts:4"]


def test_library_frames_are_skipped_even_if_present():
    text = (
        "node_modules/lib/index.js:10: boom\n"
        ".venv/lib/python3.14/site-packages/x.py:3: boom\n"
        "src/app.py:7: boom in app\n"
    )
    failures = extract.extract_failures(text, lambda _: True)
    assert [f.location for f in failures] == ["src/app.py:7"]


def test_vitest_reason_carries_expected_and_received():
    lines = reasons("vitest_fail", "src/math.test.ts")
    assert "- Expected" in lines
    assert "+ Received" in lines
    assert any(line.startswith("AssertionError: expected 2 to deeply equal 3") for line in lines)


def test_pytest_reason_carries_assertion_message():
    lines = reasons("pytest_fail", "tests/test_a.py")
    assert any("expected one side effect, got two" in line for line in lines)


def test_pytest_reasons_do_not_leak_into_the_next_failure():
    first, second = extract.extract_failures(load("pytest_fail"), exists_in("tests/test_a.py"))
    assert not any("test_eq" in line for line in first.reason)
    assert not any("expected one side effect" in line for line in second.reason)


def test_ruff_reason_is_its_own_header_not_the_neighbours():
    failures = extract.extract_failures(load("ruff_check_fail_full"), exists_in("pkg/__init__.py", "tests/test_a.py"))
    assert any("F401" in line and "`os`" in line for line in failures[1].reason)
    assert not any("`os`" in line for line in failures[0].reason)


def test_one_line_diagnostics_use_their_own_message_only():
    failures = extract.extract_failures(load("tsc_fail"), exists_in("src/bad.ts", "src/math.ts"))
    assert failures[1].reason == ("error TS2322: Type 'string' is not assignable to type 'number'.",)


def test_paths_without_line_numbers_are_reported_by_file():
    assert locations("prettier_fail", "src/math.test.ts") == ["src/math.test.ts"]


def test_prettier_syntax_error_is_attributed_to_the_file():
    failures = extract.extract_failures(load("prettier_syntax"), exists_in("lint/broken.ts"))
    assert failures[0].location == "lint/broken.ts"
    assert any("SyntaxError" in line for line in failures[0].reason)


def test_output_without_any_existing_file_falls_back_to_the_tail():
    numbered = "\n".join(f"line {i}" for i in range(50))
    (failure,) = extract.extract_failures(numbered, exists_in())
    assert failure.location is None
    assert failure.reason[0] == "line 30"
    assert failure.reason[-1] == "line 49"


def test_empty_output_reports_nothing():
    assert extract.extract_failures("", exists_in()) == ()


def test_symbols_and_colour_do_not_matter():
    plain = "app.py:3: bad thing happened here\n"
    ansi = "\x1b[31mapp.py:3:\x1b[0m bad thing happened here\n"
    for raw in (plain, ansi):
        failures = extract.extract_failures(sanitize.normalize(raw), exists_in("app.py"))
        assert failures[0].location == "app.py:3"


def test_a_different_separator_symbol_still_bounds_a_section():
    def section(rule: str) -> str:
        return (
            f"{rule * 6} first {rule * 6}\nassert a == b\nboom one\n\napp.py:1: AssertionError\n"
            f"{rule * 6} second {rule * 6}\nassert c == d\nboom two\n\napp.py:2: AssertionError\n"
        )

    for rule in ("_", "━", "="):
        first, second = extract.extract_failures(section(rule), exists_in("app.py"))
        assert "boom one" in first.reason
        assert "boom two" not in first.reason
        assert "boom two" in second.reason


def test_adjacent_references_do_not_borrow_each_others_lines():
    text = "src/a.py:3\n  Missing semicolon\nsrc/b.py:7\n  Unused variable\n"
    first, second = extract.extract_failures(text, exists_in("src/a.py", "src/b.py"))
    assert first.reason == ("Missing semicolon",)
    assert second.reason == ("Unused variable",)


def test_only_the_first_failures_get_an_explanation_when_a_limit_is_given():
    text = "".join(f"f{i}.py:1: problem number {i}\n" for i in range(5))
    failures = extract.extract_failures(text, lambda _: True, detail_limit=2)
    assert [f.reason for f in failures[2:]] == [(), (), ()]
    assert len(failures) == 5


def test_absolute_paths_are_never_reported():
    assert extract.extract_failures("/etc/passwd:1: boom here now\n", lambda _: True)[0].location is None


# Inputs that took seconds to minutes before these patterns and loops were made linear.
SLOW_BEFORE = {
    "long trailing symbol run": "app.py:1 " + "!" * 200_000 + "x\n",
    "long whitespace line": " " * 200_000 + "x\n",
    "many references in one paragraph": "".join(f"app.py:{i}: x\n" for i in range(20_000)),
    "many references each in its own paragraph": "".join(f"app.py:{i}: x\n\n" for i in range(20_000)),
}


@pytest.mark.parametrize("name", SLOW_BEFORE)
def test_extraction_time_grows_linearly_with_the_output(name: str):
    text = sanitize.normalize(SLOW_BEFORE[name])
    started = time.monotonic()
    extract.extract_failures(text, lambda _: True, detail_limit=10)
    assert time.monotonic() - started < 3


def _peak_bytes(text: str, detail_limit: int) -> int:
    tracemalloc.start()
    try:
        extract.extract_failures(text, lambda _: True, detail_limit=detail_limit)
        return tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()


def test_memory_does_not_grow_with_the_text_around_references_that_are_not_shown():
    # 30,000 references on 2 KB lines. Copying the text around each one would take over 60 MB.
    text = "".join("a.py:1 " * 300 + "\n" for _ in range(100))
    assert _peak_bytes(text, detail_limit=10) < 20_000_000


@pytest.mark.parametrize(
    ("trailer", "limit"),
    [("", 7_000_000), ("a.py:1: boom\n", 15_000_000)],
    ids=["no references", "one reference"],
)
def test_memory_per_line_stays_small_and_nothing_is_laid_out_without_references(trailer: str, limit: int):
    # A list of Python ints would take 36 bytes per line for each of three tables.
    text = "x\n\n" * 250_000 + trailer
    assert _peak_bytes(text, detail_limit=10) < limit
