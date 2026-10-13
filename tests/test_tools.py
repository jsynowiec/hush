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
        (["docker", "compose", "exec", "app", "pytest"], ("pytest", 4)),
        (["docker", "compose", "exec", "-T", "app", "uv", "run", "pytest", "-q"], ("pytest", 7)),
        (["docker-compose", "exec", "-T", "web", "pytest"], ("pytest", 4)),
        (["docker", "--context", "remote", "compose", "exec", "app", "ruff", "check", "."], ("ruff", 6)),
        (["docker", "compose", "exec", "--index=2", "app", "pytest"], ("pytest", 5)),
        (
            [
                "docker", "compose", "-f", "ci.yml", "-p", "proj", "exec", "-e", "A=1", "-u", "root",
                "-w", "/app", "app", "python", "-m", "pytest",
            ],
            ("pytest", 16),
        ),
        # The service name is not the tool, even when it is spelled like one.
        (["docker", "compose", "exec", "pytest", "sh"], ("docker", None)),
        (["docker", "compose", "exec", "vitest", "npm", "run", "test"], ("docker", None)),
        (["docker", "compose", "exec", "app", "sh", "-c", "pytest"], ("docker", None)),
        (["docker", "compose", "exec", "app"], ("docker", None)),
        # `docker exec` works the same way, with a container name instead of a service.
        (["docker", "exec", "web", "pytest"], ("pytest", 3)),
        (["docker", "exec", "-i", "web", "pytest"], ("pytest", 4)),
        (["docker", "exec", "--", "web", "pytest"], ("pytest", 4)),
        (["docker", "compose", "exec", "-T", "--", "app", "pytest"], ("pytest", 6)),
        # After the container name `--` is the command itself, which Docker cannot run.
        (["docker", "exec", "web", "--", "pytest"], ("docker", None)),
        # Detached runs return at once and `-t` needs a terminal: neither says anything about the tests.
        (["docker", "exec", "-d", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "--detach", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "-itd", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "-it", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "-ti", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "-i", "-t", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "--interactive", "--tty", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "--interactive=true", "-t", "web", "pytest"], ("docker", None)),
        # `-t` alone runs without a terminal, and so does `-i` alone.
        (["docker", "exec", "-t", "web", "pytest"], ("pytest", 4)),
        (["docker", "exec", "--tty", "web", "pytest"], ("pytest", 4)),
        (["docker", "exec", "--tty=false", "web", "pytest"], ("pytest", 4)),
        (["docker", "exec", "--interactive", "web", "pytest"], ("pytest", 4)),
        (["docker", "compose", "exec", "-d", "app", "pytest"], ("docker", None)),
        # `-d` and `-t` after the container name belong to the command, and a value is not a flag.
        (["docker", "exec", "web", "vitest", "run", "-t", "name"], ("vitest", 3)),
        (["docker", "exec", "-e", "A=-d", "web", "pytest", "-d"], ("pytest", 5)),
        (["docker", "exec", "-ed", "web", "pytest"], ("pytest", 4)),  # `-e` takes `d` as its value
        (["docker", "exec", "-w", "-t", "web", "pytest"], ("pytest", 5)),  # `-t` is the value of `-w`
        (["docker", "exec", "--detach=true", "web", "pytest"], ("docker", None)),
        (["docker", "exec", "--detach=false", "web", "pytest"], ("docker", None)),  # not worth telling apart
        (["docker", "exec", "-itu", "root", "web", "pytest"], ("docker", None)),
        # A short cluster that ends in a value option takes the next word, or the rest of the token.
        (["docker", "exec", "-iu", "root", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "-iw", "/app", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "-ie", "A=1", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "-iuroot", "web", "pytest"], ("pytest", 4)),
        (["docker", "exec", "-iu", "-t", "web", "pytest"], ("pytest", 5)),  # `-t` is the value of `-u`
        (["docker", "exec", "-iw", "-t", "web", "pytest"], ("pytest", 5)),
        (["docker", "--tlscacert", "ca.pem", "exec", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "-e", "A=1", "-w", "/app", "-u", "root", "web", "uv", "run", "pytest"], ("pytest", 11)),
        (["docker", "container", "exec", "web", "ruff", "check", "."], ("ruff", 4)),
        (["docker", "--context", "remote", "exec", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "--detach-keys", "ctrl-x", "web", "pytest"], ("pytest", 5)),
        (["docker", "exec", "compose", "pytest"], ("pytest", 3)),  # a container named compose
        (["docker", "exec", "pytest", "sh"], ("docker", None)),
        (["docker", "exec", "web"], ("docker", None)),
        (["docker", "exec", "web", "sh", "-c", "pytest"], ("docker", None)),
        # Only `exec` is looked through.
        (["docker", "compose", "run", "app", "pytest"], ("docker", None)),
        (["docker", "compose", "up", "pytest"], ("docker", None)),
        (["docker", "run", "web", "pytest"], ("docker", None)),
        (["docker", "container", "ls", "pytest"], ("docker", None)),
    ],
)  # fmt: skip
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


def test_failfast_goes_after_the_tool_inside_docker_compose_exec():
    argv = ["docker", "compose", "exec", "-T", "app", "pytest", "tests"]
    name, index = tools.find_tool(argv)
    assert tools.with_failfast(argv, name, index) == [
        "docker", "compose", "exec", "-T", "app", "pytest", "-x", "tests",
    ]  # fmt: skip


MASK = "[X]"


def _is_secret(name: str) -> bool:
    return name.endswith(("_PASSWORD", "_KEY", "TOKEN"))


@pytest.mark.parametrize(
    ("argv", "expected_env", "expected_masked"),
    [
        (
            ["docker", "exec", "-e", "DB_PASSWORD=hunter2", "web", "pytest"],
            {"DB_PASSWORD": "hunter2"},
            ["docker", "exec", "-e", "DB_PASSWORD=[X]", "web", "pytest"],
        ),
        (
            ["docker", "exec", "-eAPI_KEY=abc", "web", "pytest"],
            {"API_KEY": "abc"},
            ["docker", "exec", "-eAPI_KEY=[X]", "web", "pytest"],
        ),
        (
            ["docker", "exec", "--env=AUTH_TOKEN=abc=d", "web", "pytest"],
            {"AUTH_TOKEN": "abc=d"},
            ["docker", "exec", "--env=AUTH_TOKEN=[X]", "web", "pytest"],
        ),
        (
            ["docker", "exec", "--env", "AUTH_TOKEN=abc", "web", "pytest"],
            {"AUTH_TOKEN": "abc"},
            ["docker", "exec", "--env", "AUTH_TOKEN=[X]", "web", "pytest"],
        ),
        (
            ["docker", "exec", "-ie", "DB_PASSWORD=p", "-ieAPI_KEY=k", "web", "pytest"],
            {"DB_PASSWORD": "p", "API_KEY": "k"},
            ["docker", "exec", "-ie", "DB_PASSWORD=[X]", "-ieAPI_KEY=[X]", "web", "pytest"],
        ),
        (
            ["docker", "compose", "-f", "ci.yml", "exec", "-T", "-e", "DB_PASSWORD=p", "app", "pytest"],
            {"DB_PASSWORD": "p"},
            ["docker", "compose", "-f", "ci.yml", "exec", "-T", "-e", "DB_PASSWORD=[X]", "app", "pytest"],
        ),
        # Detached and terminal runs are not looked through, but their command line is still logged.
        (
            ["docker", "exec", "-d", "-e", "DB_PASSWORD=p", "web", "pytest"],
            {"DB_PASSWORD": "p"},
            ["docker", "exec", "-d", "-e", "DB_PASSWORD=[X]", "web", "pytest"],
        ),
        # A name that does not look secret is kept. A name with no value has nothing to mask.
        (
            ["docker", "exec", "-e", "DEBUG=1", "web", "pytest"],
            {"DEBUG": "1"},
            ["docker", "exec", "-e", "DEBUG=1", "web", "pytest"],
        ),
        (
            ["docker", "exec", "-e", "DB_PASSWORD", "web", "pytest"],
            {},
            ["docker", "exec", "-e", "DB_PASSWORD", "web", "pytest"],
        ),
        # After the container name, `-e` belongs to the command. A value of another option is not an env option.
        (
            ["docker", "exec", "web", "tool", "-e", "DB_PASSWORD=p"],
            {},
            ["docker", "exec", "web", "tool", "-e", "DB_PASSWORD=p"],
        ),
        (
            ["docker", "exec", "-u", "-e", "web", "DB_PASSWORD=p"],
            {},
            ["docker", "exec", "-u", "-e", "web", "DB_PASSWORD=p"],
        ),
        (["docker", "run", "-e", "DB_PASSWORD=p", "img"], {}, ["docker", "run", "-e", "DB_PASSWORD=p", "img"]),
        (["pytest", "-e", "DB_PASSWORD=p"], {}, ["pytest", "-e", "DB_PASSWORD=p"]),
    ],
)  # fmt: skip
def test_exec_env_values_are_found_and_masked(argv, expected_env, expected_masked):
    assert tools.exec_env(argv) == expected_env
    assert tools.mask_exec_env(argv, _is_secret, MASK) == expected_masked
