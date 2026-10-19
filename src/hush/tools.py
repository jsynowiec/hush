import posixpath
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import PurePath

# Tools with parsers. Keep this set consistent with `parsers.PARSERS`.
INNER_TOOLS = frozenset({"pytest", "ruff", "mypy", "ty", "biome", "vitest", "vite", "tsc", "eslint", "prettier"})
# Launchers such as `uv run pytest`, `pnpm exec vitest`, and `python -m pytest`.
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

# Options of `docker`, `docker compose`, `docker exec` and `compose exec` that take the next word as their value.
# An option missing here only means the tool is not found, so the command runs as it is.
_CONTAINER_VALUE_OPTIONS = frozenset(
    {
        "--context", "-c", "-H", "--host", "--config", "-l", "--log-level", "--tlscacert", "--tlscert", "--tlskey",
        "-f", "--file", "-p", "--project-name", "--profile", "--env-file", "--project-directory",
        "--ansi", "--parallel", "--progress",
        "-e", "--env", "--index", "-u", "--user", "-w", "--workdir", "--detach-keys",
    }
)  # fmt: skip
# The words that lead to the command of each program, after its own options.
_EXEC_FORMS: dict[str, tuple[tuple[str, ...], ...]] = {
    "docker": (("exec",), ("container", "exec"), ("compose", "exec")),
    "docker-compose": (("exec",),),
}

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


# `-e`, `-u`, `-w` and the like. In a short cluster such as `-iu root`, the first of these takes the rest.
_SHORT_VALUE_LETTERS = frozenset(option[1] for option in _CONTAINER_VALUE_OPTIONS if len(option) == 2)


_LONG_EXEC_FLAGS = {"--detach": "d", "--tty": "t", "--interactive": "i"}


def _split_cluster(token: str) -> tuple[str, str, str]:
    """Split a short option cluster at its first value option: (flags before it, that letter, value attached to it).

    `-iu` gives ("i", "u", ""), `-ieA=1` gives ("i", "e", "A=1") and `-it` gives ("it", "", "").
    """
    for position, letter in enumerate(token[1:], start=1):
        if letter in _SHORT_VALUE_LETTERS:
            return token[1:position], letter, token[position + 1 :]
    return token[1:], "", ""


def _exec_flags(token: str) -> set[str]:
    """Which of `d` (detach), `t` (tty) and `i` (interactive) a token sets: `-d`, `--detach`, `--tty=true`, `-it`."""
    if token.startswith("--"):
        letter = _LONG_EXEC_FLAGS.get(token.partition("=")[0])
        return {letter} if letter else set()
    return set(_split_cluster(token)[0]) & {"d", "t", "i"}


def _takes_next_word(token: str) -> bool:
    """Whether the word after this option is its value, as in `-e A=1` or `-iu root`."""
    if token.startswith("--"):
        return token in _CONTAINER_VALUE_OPTIONS
    _, letter, attached = _split_cluster(token)
    return bool(letter) and not attached


def _skip_options(argv: Sequence[str], index: int) -> tuple[int, set[str]]:
    """Skip options from `index`. Also returns the `d`, `t` and `i` flags among them.

    `--` ends the options. Docker accepts it before the container name only: after the name it is
    taken as the command.
    """
    flags: set[str] = set()
    while index < len(argv) and argv[index].startswith("-"):
        token = argv[index]
        flags |= _exec_flags(token)
        if token == "--":
            return index + 1, flags
        index += 2 if _takes_next_word(token) else 1
    return index, flags


def _exec_layout(argv: Sequence[str]) -> tuple[int, set[str]] | None:
    """For `docker exec` and `docker compose exec`: where the container or service name is, and the `d`, `t`, `i` flags.

    The index can be past the end when the name is missing.
    """
    for words in _EXEC_FORMS.get(_basename(argv[0]), ()):
        index = 1
        flags: set[str] = set()
        for word in words:
            index, found = _skip_options(argv, index)
            flags |= found
            if index >= len(argv) or argv[index] != word:
                break
            index += 1
        else:
            index, found = _skip_options(argv, index)
            return index, flags | found
    return None


def _container_command(argv: Sequence[str]) -> int | None:
    """Index of the command that `docker exec` or `docker compose exec` runs in a container, if any.

    Gives None for `-d` (the client returns at once, so the run says nothing about the tests) and for `-i`
    together with `-t` (hush has no terminal to give, so Docker refuses). Those stay plain `docker` commands.
    """
    layout = _exec_layout(argv)
    if layout is None:
        return None
    name_index, flags = layout
    command = name_index + 1
    unfit = "d" in flags or {"i", "t"} <= flags
    return None if unfit or command >= len(argv) else command


def _exec_option_values(argv: Sequence[str], long_name: str, letter: str) -> Iterator[tuple[int, int]]:
    """Where each value of a `docker exec` option sits: `argv[index][offset:]` is the value.

    `long_name` is the `--option` form and `letter` the short one, as in `--env` and `e`.
    """
    layout = _exec_layout(argv)
    if layout is None:
        return
    limit = layout[0]
    index = 1
    while index < limit:
        token = argv[index]
        if token == "--":
            return
        if not token.startswith("-"):
            index += 1  # a subcommand word such as `exec`
            continue
        if token.startswith("--"):
            option, equals, _ = token.partition("=")
            wanted, attached = option == long_name, (len(option) + 1 if equals else None)
        else:
            _, found, attached_value = _split_cluster(token)
            value = attached_value.removeprefix("=")  # Docker also reads `-w=/app`
            wanted, attached = found == letter, (len(token) - len(value) if attached_value else None)
        if wanted and attached is not None:
            yield index, attached
        elif wanted and index + 1 < limit:
            yield index + 1, 0
        index += 2 if _takes_next_word(token) else 1


def _exec_env_args(argv: Sequence[str]) -> Iterator[tuple[int, int]]:
    """Where each `-e` or `--env` value of `docker exec` sits: `argv[index][offset:]` is `NAME=VALUE` or `NAME`."""
    return _exec_option_values(argv, "--env", "e")


def exec_workdir(argv: Sequence[str]) -> str | None:
    """The absolute directory `docker exec -w` or `--workdir` sets in the container, cleaned up.

    The last one wins, as in Docker. None when there is none or it is not absolute.
    """
    values = [argv[index][offset:] for index, offset in _exec_option_values(argv, "--workdir", "w")]
    return posixpath.normpath(values[-1]) if values and posixpath.isabs(values[-1]) else None


def exec_env(argv: Sequence[str]) -> dict[str, str]:
    """The `NAME=VALUE` pairs that `docker exec -e` or `--env` gives the process in the container."""
    pairs = (argv[index][offset:].partition("=") for index, offset in _exec_env_args(argv))
    return {name: value for name, equals, value in pairs if equals}


def mask_exec_env(argv: Sequence[str], is_secret: Callable[[str], bool], placeholder: str) -> list[str]:
    """Copy argv and mask secret `-e NAME=VALUE` values, preserving option spelling."""
    masked = list(argv)
    for index, offset in _exec_env_args(argv):
        name, equals, _ = argv[index][offset:].partition("=")
        if equals and is_secret(name):
            masked[index] = f"{argv[index][:offset]}{name}={placeholder}"
    return masked


def find_tool(argv: Sequence[str]) -> tuple[str, int | None]:
    """Return (tool name, index of the tool in argv). Index is None when no inner tool is found.

    Looks through launchers and their options, and through `docker exec CONTAINER` and
    `docker compose exec SERVICE`. Stops at the first word that is not part of a launcher's own
    syntax, so `pnpm install vitest` is not a vitest run.
    """
    container_command = _container_command(argv)
    if container_command is not None:
        name, index = find_tool(argv[container_command:])
        if index is not None:
            return name, container_command + index
        return _basename(argv[0]), None
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
