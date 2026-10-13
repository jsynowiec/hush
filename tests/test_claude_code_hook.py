"""Tests for examples/claude-code/hooks/hush_nudge.py. Expected values are written by hand."""

import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from hush import tools

EXAMPLE = Path(__file__).parent.parent / "examples" / "claude-code"
HOOK = EXAMPLE / "hooks" / "hush_nudge.py"
# The hook targets the system python3 (3.9 on macOS). Use it when present so old-Python bugs show up here.
SYSTEM_PYTHON = "/usr/bin/python3" if os.access("/usr/bin/python3", os.X_OK) else sys.executable

_spec = importlib.util.spec_from_file_location("hush_nudge", HOOK)
assert _spec is not None and _spec.loader is not None
nudge = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(nudge)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _hush_bin(directory: Path) -> Path:
    """A `hush` on PATH that runs this checkout's hush, which the hook needs for `--classify`."""
    directory.mkdir(parents=True, exist_ok=True)
    _write_executable(directory / "hush", f'#!/bin/sh\nexec "{sys.executable}" -m hush "$@"\n')
    return directory


def _fake_hush(directory: Path, exit_code: int, stdout: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _write_executable(directory / "hush", f"#!/bin/sh\n[ -z '{stdout}' ] || echo '{stdout}'\nexit {exit_code}\n")
    return directory


@pytest.fixture(autouse=True)
def hush_on_path(tmp_path_factory, monkeypatch):
    bin_dir = _hush_bin(tmp_path_factory.mktemp("hush-bin"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return bin_dir


def _run_hook(payload, path, extra_env=None, stdin=None):
    env = {**os.environ, "PATH": str(path), **(extra_env or {})}
    return subprocess.run(
        [SYSTEM_PYTHON, str(HOOK)],
        input=json.dumps(payload) if stdin is None else stdin,
        capture_output=True,
        text=True,
        env=env,
    )


MAKEFILE_WITH_HUSH = "RS ?= hush\nverify:\n\t@true\n"


def _bash(command, cwd="."):
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": cwd}


# (command, expected replacement or None for "let it through")
REWRITTEN = [
    ("pytest", "hush -- pytest"),
    ("uv run pytest tests/test_x.py -k dup", "hush -- uv run pytest tests/test_x.py -k dup"),
    ("python -m pytest -q", "hush -- python -m pytest -q"),
    ("python3.12 -m pytest", "hush -- python3.12 -m pytest"),
    ("uv run ruff check .", "hush -- uv run ruff check ."),
    ("uv run ty check", "hush -- uv run ty check"),
    ("pnpm exec vitest run src/a.test.ts", "hush -- pnpm exec vitest run src/a.test.ts"),
    ("npx tsc --noEmit", "hush -- npx tsc --noEmit"),
    ("./node_modules/.bin/eslint src", "hush -- ./node_modules/.bin/eslint src"),
    ("pnpm exec vite build", "hush -- pnpm exec vite build"),
    ("pnpm test", "hush -- pnpm test"),
    ("npm t", "hush -- npm t"),
    ("npm run lint", "hush -- npm run lint"),
    ("pnpm run test:unit", "hush -- pnpm run test:unit"),
    ("bun run typecheck", "hush -- bun run typecheck"),
    ('pytest -k "dup and not slow"', "hush -- pytest -k 'dup and not slow'"),
    ("pytest -k hush", "hush -- pytest -k hush"),
    ("pytest tests/test_hush.py", "hush -- pytest tests/test_hush.py"),
    ("uv run --with pytest-xdist pytest -n 4", "hush -- uv run --with pytest-xdist pytest -n 4"),
    ("uv run --directory web pytest", "hush -- uv run --directory web pytest"),
    # The `cd` stays and hush goes after it. `--cwd` would lose locations outside the starting directory.
    ("cd web && pnpm exec biome check src", "cd web && hush -- pnpm exec biome check src"),
    ("cd web && pnpm test 2>&1 | tail -20", "cd web && hush -- pnpm test"),
    ("cd ../sibling && pytest", "cd ../sibling && hush -- pytest"),
    ("cd web/../api && uv run ruff check .", "cd web/../api && hush -- uv run ruff check ."),
    ("cd /abs/path && uv run pytest 2>&1 | tail", "cd /abs/path && hush -- uv run pytest"),
    ('cd "web" && pytest', "cd web && hush -- pytest"),
    # A word that is only an argument value is not a subcommand.
    ("uv run pytest -k server", "hush -- uv run pytest -k server"),
    ("pytest -k dev", "hush -- pytest -k dev"),
    ("pnpm exec vite build --mode dev", "hush -- pnpm exec vite build --mode dev"),
    ('pytest | grep "a;b"', "hush -- pytest"),
    # Other routine package script names.
    ("npm run check", "hush -- npm run check"),
    ("pnpm build", "hush -- pnpm build"),
    ("yarn type-check", "hush -- yarn type-check"),
    ("npm run format", "hush -- npm run format"),
    # Piping a run into an output filter throws away what hush keeps.
    ("uv run pytest -q 2>&1", "hush -- uv run pytest -q"),
    ('uv run ruff check . | grep -E "E501|F401" | head -5', "hush -- uv run ruff check ."),
]

LET_THROUGH = [
    "hush -- pytest",
    "uv run hush --name x -- uv run pytest",
    "pytest | python parse.py",
    "pytest | tail -5 > out.txt",
    "pytest | tee log.txt",
    "pytest || echo failed",
    "pytest | tail || true",
    "pytest > out.txt",
    "pytest && git push",
    'pytest | grep "\\"" | sh',
    "git log | head -5",
    'echo "a|b" | head',
    "RAW=1 pytest",
    "env CI=1 pytest",
    "pytest --help",
    "ruff --version",
    "pnpm exec vitest --watch",
    "pytest tests/*.py",
    "pytest tests/[ab].py",
    "pnpm test:watch",
    "npm run dev",
    "pnpm run test:ui",
    "pytest tests # quick check",
    "uv run pytest -k 'a 2>&1 b'",  # shell characters inside quotes: not worth the risk, so left alone
    "cd - && pytest",
    "cd -P && pytest",
    "cd $(git rev-parse --show-toplevel) && pytest",
    # Output is the point, or it is a long-running process.
    "pytest --collect-only -q",
    "pytest --co",
    "npx tsc --showConfig",
    "uv run ruff rule E501",
    "uv run ruff check --statistics .",
    "npx eslint . --format json",
    "npx eslint . --format=json",
    "pnpm exec vitest list",
    "pnpm exec vitest --reporter=json",
    "pnpm exec vite",
    "cd web && pnpm exec vite dev --port 5173",
    "pnpm exec vite preview",
    "pnpm exec biome lsp-proxy",
    # Not a routine tool run, or a tool name that is only an argument.
    "uv pip install ruff",
    "uv run python script.py ruff",
    "uv run --with pytest python bench.py",
    "uvx --with mypy python tool.py",
    "pnpm install vitest",
    "pnpm --filter web test",
    "git status",
    "ls tests",
    "",
    "pytest 'unterminated",
]


def _in_process_classify(argv):
    """What `hush --classify` answers, without starting a process: it is a thin wrapper over `find_tool`."""
    name, index = tools.find_tool(argv)
    return name if index is not None else None


@pytest.fixture
def classify_in_process(monkeypatch):
    # The table tests are about the hook's own decisions. Starting hush 70 times made them slow, and under
    # load a slow start tripped the hook's timeout, which fails open and looked like a wrong decision.
    monkeypatch.setattr(nudge, "_classify", _in_process_classify)


@pytest.mark.usefixtures("classify_in_process")
@pytest.mark.parametrize(("command", "expected"), REWRITTEN)
def test_rewrites_routine_commands(command, expected):
    assert nudge.decide(command) == expected


@pytest.mark.usefixtures("classify_in_process")
@pytest.mark.parametrize("command", LET_THROUGH)
def test_lets_other_commands_through(command):
    assert nudge.decide(command) is None


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (["uv", "run", "pytest", "tests/x.py"], "pytest"),
        (["pnpm", "exec", "vitest", "run"], "vitest"),
        (["uv", "run", "--directory", "web", "ruff", "check"], "ruff"),
        (["git", "status"], None),
        (["pnpm", "run", "test"], None),
    ],
)
def test_the_real_hush_answers_as_the_table_tests_assume(argv, expected, monkeypatch):
    monkeypatch.setattr(
        nudge, "CLASSIFY_TIMEOUT", 30
    )  # a loaded machine must not turn a slow start into a wrong answer
    assert nudge._classify(argv) == expected


def test_redirect_text_inside_quotes_is_not_stripped():
    command = "uv run pytest -k 'a 2>&1 b'"
    assert nudge._strip_output_filter(command) == command
    assert nudge._strip_output_filter("pytest 2>&1") == "pytest "


def test_environment_prefixes_never_start_hush(monkeypatch):
    def fail(argv):
        raise AssertionError("hush was started")

    monkeypatch.setattr(nudge, "_classify", fail)
    assert nudge.decide("RAW=1 pytest") is None
    assert nudge.decide("env CI=1 pytest") is None
    assert nudge.decide("") is None


def test_hush_saying_no_wins_over_a_name_it_printed(tmp_path):
    path = _fake_hush(tmp_path / "no", 1, "pytest")  # exit 1 means "not recognised", whatever was printed
    result = _run_hook(_bash("pytest"), path)
    assert (result.returncode, result.stdout) == (0, "")


def test_denies_with_the_replacement_and_the_escape_hatches(tmp_path):
    result = _run_hook(_bash("uv run ruff check .", str(tmp_path)), os.environ["PATH"])
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    reason = out["permissionDecisionReason"]
    assert "`hush -- uv run ruff check .`" in reason
    # The agent chooses fail-fast: the rewrite has none, and the reason says how to add it.
    assert "add `--failfast`" in reason
    assert "RAW=1" in reason


def test_pipe_handling_runs_on_the_system_python(tmp_path):
    """Whole-process run, so a newer-Python-only call in the pipe code fails here on 3.9."""
    result = _run_hook(_bash("uv run pytest 2>&1 | tail -5", str(tmp_path)), os.environ["PATH"])
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`hush -- uv run pytest`" in reason


def test_the_denial_names_the_repo_verify_command(tmp_path):
    (tmp_path / "Makefile").write_text(MAKEFILE_WITH_HUSH)
    result = _run_hook(_bash("uv run pytest", str(tmp_path)), os.environ["PATH"])
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert reason.endswith("For the whole project, `make verify` is already set up.")


def test_a_made_up_tool_is_decided_by_hush_alone(tmp_path):
    path = _fake_hush(tmp_path / "fake", 0, "fancytool")
    result = _run_hook(_bash("fancytool --go"), path)
    reason = json.loads(result.stdout)["hookSpecificOutput"]["permissionDecisionReason"]
    assert "`hush -- fancytool --go`" in reason


def test_hush_without_classify_lets_everything_through(tmp_path):
    path = _fake_hush(tmp_path / "old", 2)  # an old hush rejects the unknown --classify option
    result = _run_hook(_bash("uv run pytest"), path)
    assert (result.returncode, result.stdout) == (0, "")


def test_nothing_happens_when_hush_is_missing(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    result = _run_hook(_bash("pytest"), empty)
    assert (result.returncode, result.stdout) == (0, "")


def test_hook_can_be_switched_off(tmp_path):
    result = _run_hook(_bash("pytest"), os.environ["PATH"], {"HUSH_HOOK": "off"})
    assert (result.returncode, result.stdout) == (0, "")


@pytest.mark.parametrize("stdin", ["not json", "[]", "", "null"])
def test_garbage_input_never_blocks(stdin):
    result = _run_hook(None, os.environ["PATH"], stdin=stdin)
    assert (result.returncode, result.stdout) == (0, "")


def test_other_tools_are_ignored():
    payload = {"tool_name": "Read", "tool_input": {"command": "pytest"}}
    assert _run_hook(payload, os.environ["PATH"]).stdout == ""


def test_names_make_verify_when_the_makefile_uses_hush(tmp_path):
    (tmp_path / "Makefile").write_text(MAKEFILE_WITH_HUSH)
    assert nudge._repo_target(str(tmp_path)) == "make verify"
    (tmp_path / "Makefile").write_text("verify:\n\t@pytest\n")
    assert nudge._repo_target(str(tmp_path)) is None


@pytest.mark.parametrize(
    ("lockfile", "expected"),
    [
        ("pnpm-lock.yaml", "pnpm --silent verify"),
        ("package-lock.json", "npm run --silent verify"),
        ("yarn.lock", "yarn verify"),
        ("bun.lock", "bun run --silent verify"),
        ("bun.lockb", "bun run --silent verify"),
    ],
)
def test_names_the_package_verify_script(tmp_path, lockfile, expected):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"verify": "hush -- vitest run"}}))
    (tmp_path / lockfile).write_text("")
    assert nudge._repo_target(str(tmp_path)) == expected


def test_package_verify_script_without_hush_is_not_named(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"verify": "vitest run"}}))
    assert nudge._repo_target(str(tmp_path)) is None


def test_finds_the_verify_target_from_a_subdirectory(tmp_path):
    (tmp_path / "Makefile").write_text(MAKEFILE_WITH_HUSH)
    sub = tmp_path / "web" / "src"
    sub.mkdir(parents=True)
    assert nudge._repo_target(str(sub)) == "make verify"


def test_search_stops_at_the_repository_root(tmp_path):
    (tmp_path / "Makefile").write_text(MAKEFILE_WITH_HUSH)
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert nudge._repo_target(str(repo)) is None


def _handler_rules(settings_file: str) -> list[str]:
    settings = json.loads((EXAMPLE / settings_file).read_text())
    return [h["if"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]]


@pytest.mark.parametrize("settings_file", ["settings.global.json", "settings.project.json"])
def test_prefilter_covers_every_tool_launcher_and_package_manager(settings_file):
    """The `if` rules only save spawning the hook; a missing one means a routine command is never seen."""
    rules = _handler_rules(settings_file)
    first_words = {rule.removeprefix("Bash(").removesuffix(")").removesuffix(" *") for rule in rules}
    wanted = set(tools.INNER_TOOLS) | set(tools._LAUNCHERS) | set(nudge.PACKAGE_MANAGERS)
    assert wanted - first_words == set()
    assert "python*" in first_words  # stands for python, python3, python3.12 ...
    for tool in tools.INNER_TOOLS:
        # A rule with two wildcards does not match a bare command, so a path to a tool needs both forms.
        assert f"Bash(*/{tool} *)" in rules, f"no rule for a path to {tool}"
        assert f"Bash(*/{tool})" in rules, f"no rule for a bare path to {tool}"


def test_global_and_project_settings_differ_only_in_the_hook_command():
    def without_command(settings_file):
        settings = json.loads((EXAMPLE / settings_file).read_text())
        for entry in settings["hooks"]["PreToolUse"]:
            for handler in entry["hooks"]:
                handler.pop("command")
        return settings

    assert without_command("settings.global.json") == without_command("settings.project.json")


@pytest.mark.parametrize("settings_file", ["settings.global.json", "settings.project.json"])
def test_a_missing_hook_file_does_not_block_commands(settings_file, tmp_path):
    """python3 exits 2 for a missing script, and exit 2 blocks the tool call. The command must absorb that."""
    settings = json.loads((EXAMPLE / settings_file).read_text())
    commands = {h["command"] for entry in settings["hooks"]["PreToolUse"] for h in entry["hooks"]}
    assert len(commands) == 1
    env = {**os.environ, "HOME": str(tmp_path), "CLAUDE_PROJECT_DIR": str(tmp_path), "PATH": os.environ["PATH"]}
    result = subprocess.run(["sh", "-c", commands.pop()], input="{}", capture_output=True, text=True, env=env)
    assert (result.returncode, result.stdout) == (0, "")
