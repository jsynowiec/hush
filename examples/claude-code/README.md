# Using hush from Claude Code

A hook that sends routine test, lint, type check and build commands through `hush`, so the agent sees one line on success and `path:line` on failure instead of pages of output.

It works by denying the bare command and telling the agent the exact `hush -- ...` command to run instead. The agent retries with that command. The hook never answers "allow", so a mistake in it can only cause a wrong denial, never skip a permission check. Every error path in the script ends in "do nothing".

## Files

| File | Purpose |
| --- | --- |
| `hooks/hush_nudge.py` | The hook. Standard library only, Python 3.9 or newer. |
| `settings.global.json` | Registers the hook for every repo (`~/.claude/settings.json`). |
| `settings.project.json` | Registers the hook for one repo (`.claude/settings.json`). Same rules, different command path. |
| `permissions.allow.json` | Optional allow rules so the rewritten commands do not prompt. Read the Permissions section first. |
| `CLAUDE.md.snippet` | Guidance for `~/.claude/CLAUDE.md`. |
| `AGENTS.md.snippet` | Guidance for a repo's `AGENTS.md`. |

The hook needs hush 0.2.0 or newer, for `hush --classify`.

## Install for all repos

1. Install hush from a clone of this repo: `uv tool install .` (add `--reinstall` to update).
2. `mkdir -p ~/.claude/hooks && cp examples/claude-code/hooks/hush_nudge.py ~/.claude/hooks/`
3. Merge the `hooks` entry of `settings.global.json` into `~/.claude/settings.json`. If you already have `PreToolUse` hooks, add these entries to the list.
4. Append `CLAUDE.md.snippet` to `~/.claude/CLAUDE.md`.
5. Add `.tmp/` to your global gitignore, or other repos show `.tmp/test-results` as untracked.
6. Optional: see Permissions.

## Install for one repo

1. Copy the hook to `.claude/hooks/hush_nudge.py` in the repo.
2. Merge `settings.project.json` into `.claude/settings.json` and commit both.
3. Add the verify step. For a Makefile repo copy `examples/Makefile`. For a JavaScript repo add a script next to the existing ones and leave `test`, `lint` and `typecheck` alone, because people and CI depend on their output:

   ```json
   "verify": "hush --name 'Lint and format' -- biome check . && hush --name 'Type checks' -- tsc --noEmit && hush --name 'Unit tests' -- vitest run"
   ```

   Run it as `pnpm --silent verify`, `npm run --silent verify` or `bun run --silent verify` so the package manager prints no banner. `&&` makes it stop at the first failure.
4. Add `AGENTS.md.snippet` to the repo's `AGENTS.md`, and `.tmp/` to its `.gitignore`.

Things to know about the project install:

- Claude Code only reads the repo's `.claude/settings.json` when the session starts in the directory that holds it. Checked with `claude -p` 2.1.287: a session started in `repo/web` did not load `repo/.claude/settings.json`, and `$CLAUDE_PROJECT_DIR` was `repo/web`. For monorepos where people start sessions in a package folder, use the global install.
- Install the hook once, either for all repos or for one repo. Hooks from both places run, so the agent would be denied twice.
- Teammates without hush are not blocked: the hook does nothing when `hush` is not on `PATH`. A `verify` script that calls `hush` does fail for them, so commit it only where everyone has hush.
- Each settings command ends in `|| true`. Without it, a missing script file makes `python3` exit with code 2, and for a `PreToolUse` hook exit code 2 blocks the command. That would block every `uv`, `npm`, `pnpm`, `python` and tool command for anyone who merged the settings but did not copy the script.

## What it rewrites and what it leaves alone

It rewrites:

- Known tools, looked up by `hush --classify`: `pytest`, `uv run ruff check .`, `pnpm exec vitest run`, `npx tsc --noEmit`, `python -m pytest`, and paths to them such as `./node_modules/.bin/eslint`.
- A tool run in a container with `docker exec`, `docker compose exec` or `docker-compose exec`, such as `docker exec web pytest`. `hush` does not look through `-d` (detached) or `-i` together with `-t` (needs a terminal, which neither the agent nor `hush` has), so those commands pass through. The agent's own run of `docker exec -it` then fails with Docker's message, and its retry without `-t` is rewritten.
- Package scripts that hide the tool: `pnpm test`, `npm t`, `npm run test:unit`, `npm run lint`, `pnpm build` and the like.
- A leading `cd`: `cd web && pnpm test` becomes `cd web && hush -- pnpm test`. The `cd` stays because hush only reports file locations under the directory it started in, so `--cwd` would lose them for `cd ../x`.

The suggestion has no `--failfast`. The agent chooses: quick feedback or a full list for final verification.

Before judging, it strips `2>&1` and trailing `| head`, `| tail`, `| grep`, `| egrep`, `| wc`, `| cat`, `| sed`, `| awk`, `| cut`, `| sort` and `| uniq`, because the pipe throws away the failure detail hush keeps. If the count from `| wc -l` is the point, prefix the command with `RAW=1`.

It leaves alone:

- Anything with shell syntax such as `&&` after the first part, `;`, redirects, globs, brackets and `$()`, and any command with a backslash or a `#` comment.
- Environment prefixes (`CI=1 pytest`), `env`, and wrappers such as `timeout`, `time`, `nice` and `command`. Checked: the settings rules do not start the hook for wrapped commands, and hush does not see through them either.
- Commands already using hush, and `--help` and `--version`.
- Output that is the point: `--collect-only`, `--showConfig`, `ruff rule`, `--format json`, `--reporter=json`, `vitest list`.
- Dev servers and watchers: `vite`, `vite dev`, `vite preview`, `lsp-proxy`, `--watch`, and scripts such as `test:watch` or `dev`. hush prints only after the command exits, so a long-running command would show nothing.
- Words that are only argument values: `pytest -k server` is still rewritten.

When the repo has a `make verify`, or a `verify` script run through a package manager, that uses hush, the denial names it.

## Switches

- `HUSH_HOOK=off` in the environment turns the hook off.
- Prefix a command with an environment variable, such as `RAW=1 pytest --collect-only`, to run it bare on purpose.
- With no `hush` on `PATH`, or a hush without `--classify`, the hook does nothing.

## Subagents

Settings hooks also run inside subagents (checked: the hook payload carries `agent_id` and `agent_type`, and a denied Explore subagent retried with the hush command). The built-in Explore and Plan agents skip CLAUDE.md, so the denial text names `RAW=1` itself. A subagent whose hush command is blocked by a permission prompt may fall back to `RAW=1`, which is the escape hatch working as designed.

## Cost

Each `if` rule in the settings files makes Claude Code start the hook only for commands that begin with a launcher, a known tool, or a path to one. A command such as `git status` or `rg foo` never starts it. Checked on Claude Code 2.1.287: the rules matched compound commands (`cd x && uv ...`), env prefixes, pipes, bare commands and bare paths. A rule with two wildcards does not match a bare command, so paths to tools have a second rule without arguments.

When the hook does start, it costs roughly 80 to 100 ms: about 20 ms for `python3` and about 40 ms for `hush --classify`. A command the hook can dismiss without asking hush costs about 30 ms. The rules are only a speed-up: if one is missing the command is simply not rewritten, and a test fails if a tool, launcher or package manager known to hush has no rule. Hooks that your organisation installs system-wide add their own time to every tool call, separate from this one.

The hook leaves a command alone when one of its words is in `SKIP_ARGS` in `hooks/hush_nudge.py` (help and version flags, `-w`, `server`, `dev`, `start`, `watch`, `list`, `ui` and the like). For `docker` and `docker-compose` commands only the words from the tool onward are checked, so a container, service, user or `-w DIR` value named like one of those words does not stop the rewrite, while `docker exec web vitest --watch` is still left alone. A skipped word that sits before the tool, such as `docker exec web node --watch ./node_modules/.bin/vitest run`, is not seen. Docker commands without `exec` (`docker ps`, `docker build`) are dismissed without starting `hush`.

## Permissions

`permissions.allow.json` lets the rewritten commands, such as `hush -- uv run pytest *`, run without a prompt. It is optional, and it has limits:

- It has no effect where your organisation sets `allowManagedPermissionRulesOnly`. On such a machine only the managed allow list counts, so ask your administrator whether `make verify` or the hush commands are on it. If `make verify` is allowed, that route runs without a prompt.
- Whether these rules match exactly the commands the hook suggests was not verified.
- The file has no rules for `docker exec` or `docker compose exec`, on purpose. A rule such as `hush -- docker exec *` would approve any command in any container, for the same reason `--cwd *` is excluded below. Rewritten container commands still ask for permission.
- Each rule allows whatever its tool can be told to run, the same as allowing the tool bare. For example `pytest -p some_module`, `eslint -c evil.js`, `prettier --plugin` and `npm run build` run code. Allow only tools you would allow without hush.
- Never allow `Bash(hush *)` or any `--cwd *` form. `*` stands for any text, so `hush --cwd . -- sh -c '...'` would match, and hush runs whatever it is given. The hook does not suggest `--cwd` for this reason.
- Claude Code splits compound commands and checks each part, so a rule for a hush command does not cover `; rm -rf`.

## Safety notes

- The hook runs the first `hush` found on `PATH`, before any permission prompt. A repo that puts its own `bin` directory first on `PATH` (direnv, an activated virtualenv) could supply a different `hush`. This is the same exposure as any program resolved from `PATH`.
- `hush --classify` runs nothing and writes nothing. Words that look like options, such as `--help` or `--log-dir`, are passed after `--` and are treated as part of the command.

## Logs

Each run writes its own log file in `.tmp/test-results`, and hush prunes old ones. See `--keep-logs` in the main README.

## Checked and not checked

Checked:

- The hook's decisions, in `tests/test_claude_code_hook.py`. The decision tables call the function directly, with `hush --classify` answered by the same `find_tool` function. A smaller set runs the script as a separate process on the system `python3` (3.9 on macOS), and a few ask the real hush.
- With `claude -p` 2.1.287: the denial reaches the agent and it retries with the hush command; `if` matching; hooks in subagents; the settings location when starting in a subfolder; `make verify` with hush inside, passing and failing, through a project-level install.
- The quiet flags: `npm run --silent`, `pnpm --silent` and `bun run --silent` work. `pnpm -s` does not silence the banner and `bun run -s` is an error. Yarn was not installed, so it is run without a flag.

Not checked:

- That the rules in `permissions.allow.json` match the commands the hook suggests.
- A complete run of a rewritten `hush -- ...` command with the permission prompt answered.
- Whether a denial still applies in `bypassPermissions` mode.
- Interactive sessions. Everything above used `claude -p`.
