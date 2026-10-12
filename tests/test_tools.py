import pytest

from hush import parsers, tools


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["pytest", "-q"], ("pytest", 0)),
        (["uv", "run", "pytest", "tests/"], ("pytest", 2)),
        (["uv", "run", "--frozen", "ruff", "check", "."], ("ruff", 3)),
        (["python", "-m", "pytest"], ("pytest", 2)),
        (["pnpm", "exec", "vitest", "run"], ("vitest", 2)),
        (["pnpm", "exec", "eslint", "."], ("eslint", 2)),
        (["bunx", "prettier", "--check", "."], ("prettier", 1)),
        (["./node_modules/.bin/tsc", "--noEmit"], ("tsc", 0)),
        (["uv", "run", "python", "-m", "pytest"], ("pytest", 4)),
        (["uv", "run", "--with", "numpy", "pytest"], ("pytest", 4)),
        (["uv", "run", "--with", "pytest", "ruff", "check"], ("ruff", 4)),
        (["uv", "run", "--project", "sub", "pytest"], ("pytest", 4)),
        (["python3.12", "-m", "pytest"], ("pytest", 2)),
        (["pnpm", "--filter", "vite", "exec", "tsc"], ("tsc", 4)),
        (["pnpm", "-C", "web", "exec", "vitest", "run"], ("vitest", 4)),
        (["npx", "--yes", "eslint", "."], ("eslint", 2)),
        (["pnpm", "run", "test"], ("pnpm", None)),
        (["make", "test"], ("make", None)),
    ],
)
def test_find_tool(argv: list[str], expected: tuple[str, int | None]):
    assert tools.find_tool(argv) == expected


def test_a_tool_name_that_is_only_an_install_argument_is_not_the_tool():
    assert tools.find_tool(["pnpm", "install", "vitest"]) == ("pnpm", None)
    assert tools.find_tool(["uv", "add", "--dev", "pytest"]) == ("uv", None)


def test_failfast_inserts_pytest_flag_right_after_the_tool():
    argv = ["uv", "run", "pytest", "tests/", "-q"]
    assert tools.with_failfast(argv, "pytest", 2) == [
        "uv",
        "run",
        "pytest",
        "-x",
        "tests/",
        "-q",
    ]


def test_failfast_inserts_vitest_bail():
    argv = ["pnpm", "exec", "vitest", "run"]
    assert tools.with_failfast(argv, "vitest", 2) == [
        "pnpm",
        "exec",
        "vitest",
        "--bail=1",
        "run",
    ]


@pytest.mark.parametrize("existing", ["-x", "--exitfirst", "--maxfail=3"])
def test_failfast_respects_a_stop_policy_the_caller_already_set(existing: str):
    argv = ["pytest", existing]
    assert tools.with_failfast(argv, "pytest", 0) == argv


def test_failfast_respects_existing_vitest_bail():
    argv = ["vitest", "run", "--bail=3"]
    assert tools.with_failfast(argv, "vitest", 0) == argv


@pytest.mark.parametrize("tool", ["ruff", "tsc", "biome", "mypy", "eslint"])
def test_tools_without_a_failfast_flag_are_unchanged(tool: str):
    argv = [tool, "."]
    assert tools.with_failfast(argv, tool, 0) == argv


def test_failfast_does_nothing_when_the_tool_is_hidden_behind_a_script():
    argv = ["pnpm", "run", "test"]
    assert tools.with_failfast(argv, "pnpm", None) == argv


def test_every_parsed_tool_can_be_recognised_in_a_command():
    assert set(parsers.PARSERS) == tools.INNER_TOOLS


def test_failfast_goes_after_the_tool_not_after_a_launcher_option_value():
    argv = ["uv", "run", "--with", "pytest", "ruff", "check"]
    name, index = tools.find_tool(argv)
    assert tools.with_failfast(argv, name, index) == argv
    argv = ["uv", "run", "--project", "sub", "pytest", "-q"]
    name, index = tools.find_tool(argv)
    assert tools.with_failfast(argv, name, index) == [
        "uv", "run", "--project", "sub", "pytest", "-x", "-q",
    ]  # fmt: skip
