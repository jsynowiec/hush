import re
from collections.abc import Sequence
from pathlib import PurePath

# Tools hush has a parser for. Keep in step with `parsers.PARSERS` (a test checks this).
INNER_TOOLS = frozenset({"pytest", "ruff", "mypy", "ty", "biome", "vitest", "vite", "tsc", "eslint", "prettier"})
# Programs that start another program, e.g. `uv run pytest`, `pnpm exec vitest`, `python -m pytest`.
_LAUNCHERS = frozenset({"uv", "uvx", "pnpm", "npx", "bunx", "bun", "node"})
_PYTHON = re.compile(r"^python(?:\d+(?:\.\d+)*)?$")
# Words a launcher accepts before the tool name. Anything else (`install`, `add`) ends the search.
_LAUNCHER_WORDS = frozenset({"run", "exec", "dlx", "x"})
# Launcher options that take the next word as their value, which must not be mistaken for a tool.
_VALUE_OPTIONS = frozenset(
    {
        "--with", "--with-requirements", "--with-editable", "--project", "--directory", "--python",
        "--extra", "--group", "--package", "--env-file", "--index", "--filter", "--dir", "--prefix",
        "-C", "-p", "-P", "-w",
    }
)  # fmt: skip

# Per-tool fail-fast flag, and the flags that mean the caller already chose a stop policy.
_FAILFAST_FLAG: dict[str, str] = {"pytest": "-x", "vitest": "--bail=1"}
_FAILFAST_ALREADY_SET: dict[str, tuple[str, ...]] = {
    "pytest": ("-x", "--exitfirst", "--maxfail"),
    "vitest": ("--bail",),
}


def _basename(token: str) -> str:
    return PurePath(token).name


def _is_launcher(name: str) -> bool:
    return name in _LAUNCHERS or bool(_PYTHON.match(name))


def find_tool(argv: Sequence[str]) -> tuple[str, int | None]:
    """Return (tool name, index of the tool in argv). Index is None when no inner tool is found.

    Looks through launchers and their options. Stops at the first word that is not part of a
    launcher's own syntax, so `pnpm install vitest` is not a vitest run.
    """
    index = 0
    while index < len(argv):
        token = argv[index]
        name = _basename(token)
        if name in INNER_TOOLS:
            return name, index
        if index == 0 and not _is_launcher(name):
            break
        if token in _VALUE_OPTIONS:
            index += 1
        elif not (token.startswith("-") or token in _LAUNCHER_WORDS or _is_launcher(name)):
            break
        index += 1
    return _basename(argv[0]), None


def with_failfast(argv: Sequence[str], tool: str, tool_index: int | None) -> list[str]:
    """Insert the tool's fail-fast flag unless the caller already set a stop policy.

    Does nothing when the tool is not visible in the command, for example `pnpm run test`.
    """
    flag = _FAILFAST_FLAG.get(tool)
    if flag is None or tool_index is None:
        return list(argv)
    already_set = _FAILFAST_ALREADY_SET[tool]
    if any(token == s or token.startswith(f"{s}=") for token in argv for s in already_set):
        return list(argv)
    return [*argv[: tool_index + 1], flag, *argv[tool_index + 1 :]]
