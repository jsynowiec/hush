#!/usr/bin/env python3
"""Claude Code PreToolUse hook for Bash. Steers routine test, lint and build commands through hush.

It denies a bare `pytest`, `uv run ruff check .`, `pnpm test` and similar, and tells the agent the exact
`hush -- ...` command to run instead. Everything it is unsure about passes through untouched, and it never
answers "allow", so a mistake here can only cause a wrong denial, never skip a permission check.

`hush --classify` decides which commands are known tools, so hush stays the only place that lists tools and
launchers. A hush without that flag exits 2, which here means "not recognised". Package scripts such as
`pnpm test` hide the tool, so this hook recognises those itself.

Stdlib only and Python 3.9 compatible, because it runs on the system python3.
"""

import json
import os
import re
import shlex
import shutil
import subprocess
import sys

CLASSIFY_TIMEOUT = 2  # seconds; a hush that is slower than this counts as "not recognised"
PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
# Commands that run another command in a container. Their own words (container, user, `-w DIR`) are not the
# tool's arguments.
CONTAINER_COMMANDS = {"docker", "docker-compose"}
# Routine package scripts, with optional suffixes such as `test:unit`. `t` is the npm alias for `test`.
SCRIPT = re.compile(r"^(t|test|lint|typecheck|type-check|check|build|format)(:.+)?$")
# Arguments and subcommands whose output is the point, or that start a long-running process.
SKIP_ARGS = {
    "-h", "--help", "-V", "--version", "--watch", "-w", "--ui", "--init", "--lsp", "--serve",
    "server", "watch", "start", "list", "init", "bench", "rule", "lsp-proxy", "preview", "dev", "serve", "ui",
    "--collect-only", "--co", "--fixtures", "--markers", "--showConfig", "--listFiles", "--listEmittedFiles",
    "--statistics", "--show-settings", "--print-config", "--json",
}  # fmt: skip
# Flags whose next word is a value (`pytest -k server`), which must not be mistaken for a subcommand.
VALUE_FLAGS = {"-k", "-m", "-t", "--mode", "--grep"}
# `--format json`, `--reporter=json` and the like change the output on purpose.
OUTPUT_FORMAT = re.compile(r"^--(format|reporter|output-format)(=|$)")
# Output filters an agent pipes a run into. The pipe throws away the failure detail hush keeps.
# `tee` is left out: stripping it would silently drop the file the agent asked for.
FILTERS = {"head", "tail", "grep", "egrep", "wc", "cat", "sed", "awk", "cut", "sort", "uniq"}
# Anything the shell would interpret. A command containing these is not a single simple command.
SHELL_SYNTAX = re.compile(r"[|&;<>`$(){}\[\]\n*?~\\]")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")
STAGE_SYNTAX = re.compile(r"[;&<>`$()\n]")
COMMENT = re.compile(r"(^|\s)#")
# Lockfile to the command that runs a package script without the package manager's banner.
# Checked with pnpm 12, bun 1.4 and npm 11. `pnpm -s` is not silent and `bun run -s` is an error. Yarn is
# not checked, so it gets no flag.
LOCKFILES = (
    ("pnpm-lock.yaml", "pnpm --silent"),
    ("bun.lock", "bun run --silent"),
    ("bun.lockb", "bun run --silent"),
    ("yarn.lock", "yarn"),
    ("package-lock.json", "npm run --silent"),
)


def _unquoted_pipes(command):
    """Indexes of `|` that are outside quotes."""
    pipes, quote = [], None
    for index, char in enumerate(command):
        if quote:
            quote = None if char == quote else quote
        elif char in "'\"":
            quote = char
        elif char == "|":
            pipes.append(index)
    return pipes


def _strip_output_filter(command):
    """Drop `2>&1` and trailing `| head|tail|grep ...` stages so the run itself can be judged.

    `2>&1` inside quotes is left alone. Returns the command without `2>&1` but otherwise unchanged when a
    pipe leads anywhere else.
    """
    chunks = re.split(r"('[^']*'|\"[^\"]*\")", command)
    command = "".join(c if i % 2 else re.sub(r"(?<!\S)2>&1(?!\S)", "", c) for i, c in enumerate(chunks))
    pipes = _unquoted_pipes(command)
    if not pipes:
        return command
    ends = [*pipes[1:], len(command)]
    # No zip(strict=...): that keyword needs Python 3.10 and this runs on 3.9.
    for index, start in enumerate(pipes):
        stage = command[start + 1 : ends[index]].strip()
        first = stage.split(None, 1)[0] if stage else ""
        if first not in FILTERS or STAGE_SYNTAX.search(QUOTED.sub("", stage)):
            return command
    return command[: pipes[0]].strip()


def _split_cd(command):
    """`cd DIR && REST` -> (DIR, REST). Anything else, or a DIR that is not a plain path, -> (None, command)."""
    match = re.match(r"^\s*cd\s+(\S+)\s*&&\s*(.+)$", command, re.DOTALL)
    if not match:
        return None, command
    try:
        words = shlex.split(match.group(1))
    except ValueError:
        return None, command
    # `cd -` and `cd -P` are not directories, and anything the shell would expand is not a plain path.
    if len(words) != 1 or words[0].startswith("-") or SHELL_SYNTAX.search(words[0]):
        return None, command
    return words[0], match.group(2)


def _classify(argv):
    """The tool name hush recognises in this command, or None. Any failure counts as None."""
    try:
        result = subprocess.run(
            ["hush", "--classify", "--", *argv], capture_output=True, text=True, timeout=CLASSIFY_TIMEOUT
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return (result.stdout.strip() or None) if result.returncode == 0 else None


def _is_package_script(argv):
    if argv[0] not in PACKAGE_MANAGERS or len(argv) < 2:
        return False
    rest = argv[1:]
    if rest[0] == "run" and len(rest) > 1:
        rest = rest[1:]
    # `test:watch` and `dev:server` are long-running, whatever the first part says.
    return bool(SCRIPT.match(rest[0])) and not any(part in SKIP_ARGS for part in rest[0].split(":"))


def _skips(argv):
    """Whether an argument asks for informational output or a long-running process. Flag values are ignored."""
    previous = ""
    for arg in argv:
        if previous not in VALUE_FLAGS and (arg in SKIP_ARGS or OUTPUT_FORMAT.match(arg)):
            return True
        previous = arg
    return False


def decide(command):
    """Return the replacement command line for a routine command, or None to let it through."""
    if "\\" in command or COMMENT.search(command):
        return None
    cd_dir, command = _split_cd(command)
    command = _strip_output_filter(command)
    if SHELL_SYNTAX.search(command):
        return None
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    if not argv or "=" in argv[0] or argv[0] == "env":
        # Empty, or an environment prefix, which is also the deliberate way to run a tool raw. hush would say
        # "not recognised" too; this saves starting it.
        return None
    in_container = os.path.basename(argv[0]) in CONTAINER_COMMANDS
    if in_container and "exec" not in argv:
        return None  # `docker ps`, `docker build`, `docker compose up`: nothing to look through
    if not in_container and _skips(argv):
        return None
    tool = _classify(argv)
    if in_container and tool is not None:
        # Only the tool's own arguments count: `docker exec -w /app server pytest` is a routine run.
        first = next((i for i, arg in enumerate(argv) if os.path.basename(arg) == tool), 0)
        if _skips(argv[first:]):
            return None
    if tool == "vite" and "build" not in argv:
        return None  # only `vite build` is a routine run; bare `vite` is a dev server
    if tool is None and not _is_package_script(argv):
        return None
    hush = shlex.join(["hush", "--", *argv])
    # Move with `cd` rather than `--cwd`: hush only reports files under the directory it started in.
    return f"cd {shlex.quote(cd_dir)} && {hush}" if cd_dir is not None else hush


def _read(path):
    try:
        with open(path, encoding="utf-8") as handle:
            return handle.read()
    except OSError:
        return ""


def _repo_target(cwd):
    """The whole-project verify command when a repo, at `cwd` or up to 5 levels above, already uses hush for it."""
    directory = os.path.abspath(cwd)
    for _ in range(6):
        makefile = _read(os.path.join(directory, "Makefile"))
        if re.search(r"^verify:", makefile, re.M) and "hush" in makefile:
            return "make verify"
        try:
            verify = json.loads(_read(os.path.join(directory, "package.json")) or "{}").get("scripts", {}).get("verify")
        except (ValueError, AttributeError):
            verify = None
        if "hush" in str(verify):
            found = (cmd for lock, cmd in LOCKFILES if os.path.exists(os.path.join(directory, lock)))
            return f"{next(found, 'npm run -s')} verify"
        parent = os.path.dirname(directory)
        if parent == directory or os.path.exists(os.path.join(directory, ".git")):
            break
        directory = parent
    return None


def _reason(replacement, cwd):
    reason = (
        f"Run this through hush to save context: `{replacement}`. "
        "It prints one line on success and `path:line` plus a short reason on failure, and keeps the full log "
        "in .tmp/test-results. Read the log only if the summary is not enough. "
        "Do not pipe it into head or tail; hush already keeps the useful part. "
        "For pytest and vitest named in the command (not hidden behind `pnpm test`), add `--failfast` before `--` "
        "to stop at the first failure when you want quick feedback; leave it off for final verification so "
        "every failure is listed. "
        "If you need the tool's own output (listing tests, JSON, a server), prefix the command with `RAW=1`."
    )
    target = _repo_target(cwd)
    return f"{reason} For the whole project, `{target}` is already set up." if target else reason


def main():
    if os.environ.get("HUSH_HOOK") == "off" or shutil.which("hush") is None:
        return 0  # without hush on PATH the advice cannot be followed, so never block
    payload = json.load(sys.stdin)
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command") or ""
    replacement = decide(command)
    if replacement is None:
        return 0
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": _reason(replacement, payload.get("cwd") or os.getcwd()),
            }
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # a broken hook must never block the agent's work
        sys.exit(0)
