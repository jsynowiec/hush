# hush

Noisy build and test output wastes a coding agent's context window. `hush` runs one command and prints a single line if it passes. If it fails, it prints where it failed (`path:line`), a few lines of explanation, and the path to the full log.

```
  ✔ PASS (pytest) Unit tests passed (318 tests, 4 skipped, 1 xfailed, 2 warnings); 12.4s
```

```
  ✗ FAIL (vitest) Unit tests failed; 0.3s

    src/math.test.ts::dup:6
    Reason:
      AssertionError: expected one side effect: expected 2 to be 1 // Object.is equality
      - Expected
      + Received
      - 1
      + 2

    Details: .tmp/test-results/vitest-unit-tests-20260512T091530-a3f9.log
```

## Install

`hush` needs Python 3.12 or newer and has no dependencies. From a clone of this repo, `uv tool install .` puts `hush` on your `PATH`. Without installing, run it as `uv run hush` from the repo.

## Use

Put the command after `--`:

```
hush --name "Lint check" -- uv run ruff check .
hush --name "Unit tests" --failfast -- uv run pytest
hush --name "Build" --cwd module-b -- pnpm exec vite build
```

The tool label in brackets (`(ruff)`, `(pytest)`) is guessed from the command, including through `uv run`, `uvx`, `pnpm exec`, `bunx`, `bun`, `npx`, `node` and `python -m`. Use `--tool` to set it by hand.

| Option | What it does |
| --- | --- |
| `--name TEXT` | Describes the step in the output, for example "Lint check". |
| `--tool TEXT` | Tool name for the label and for choosing the output parser, when it cannot be guessed. |
| `--cwd DIR` | Runs the command in `DIR`. Reported paths and the log path stay relative to where you ran `hush`, so they make sense from the repo root. |
| `--failfast` / `--no-failfast` | Adds the tool's own stop-at-first-failure flag. Only `pytest` (`-x`) and `vitest` (`--bail=1`) have one. Skipped if you already set a stop flag, and when the tool is not visible in the command (`pnpm run test` hides it, so put the flag in the script). Default is off. |
| `--timeout SECONDS` | Stops the command and its child processes when time runs out. Exit code 124. Must be above 0. |
| `--log-dir DIR` | Where full logs go. Default `.tmp/test-results`. Every run writes its own file, named after the step plus the run's UTC start time and 4 random characters, for example `pytest-unit-tests-20260512T091530-a3f9.log`, so runs of the same step never overwrite each other. |
| `--keep-logs N` | After each run, keeps the newest `N` logs in `--log-dir` (by modification time, across all steps) and deletes the rest. Default 40. At least 1. Only files named like `hush` logs are ever deleted, and the log of the current run never is. A suite with more steps than `N` loses the logs of its earliest steps, so raise `N` for it. A failure while deleting is ignored. Nothing is deleted when the log could not be written. |
| `--max-failures N` | Failures to list before saying "and N more". Default 10. At least 1. |
| `--max-capture-mb N` | Output kept in memory and in the log. Default 10. When a run prints more, the first half and the last half are kept, because the failure summary is usually last. At least 1. |
| `--color auto\|always\|never` | Colours the `✔ PASS` mark green and the `✗ FAIL` mark red. `auto` (the default) does it only on a terminal and not when `NO_COLOR` is set, so agents never get colour codes. The log is never coloured. |
| `--verbose` | After the command ends, also prints its cleaned and redacted output to stderr. |
| `--classify` | Does not run the command. Prints the tool `hush` recognises in it and exits 0, or prints nothing and exits 1. See "Ask whether a command is a known tool". |

Except with `--classify`, the exit code is the command's own exit code. The run counts as passed or failed by that code alone, never by what the output says. `hush` adds three codes of its own: 124 for a timeout, 126 when the command is found but cannot be run, and 127 when it is not found.

## Run a single tool

You do not need a Makefile. Any one command works, which is the right call for an agent that wants a scoped run:

```
hush --failfast -- uv run pytest tests/test_retries.py
hush -- uv run pytest tests/test_retries.py::test_duplicate_execution
hush --failfast -- uv run pytest tests/ -k "retry and not slow"
hush -- pnpm exec vitest run src/math.test.ts
hush -- uv run ruff check src/retries.py
```

With no `--name`, the pass line is `✔ PASS (pytest) passed (3 tests); 1.2s`, and the log is `.tmp/test-results/pytest-<date>T<time>-<id>.log`, unique to this run. Give `--name` to put the step in the file name, which makes the logs easier to tell apart.

## Ask whether a command is a known tool

Other programs, such as an agent hook that decides whether to route a command through `hush`, can ask `hush` instead of keeping their own list of tools and launchers:

```
$ hush --classify -- uv run pytest tests/test_retries.py
pytest
$ hush --classify -- pnpm run test
$ hush --classify -- git status
```

With `--classify`, `hush` does not run the command and writes no log. It looks for a tool the same way the label is guessed, through `uv run`, `pnpm exec`, `python -m` and the other launchers. The answer comes from the command alone, so `--tool` is ignored and `--cwd` is not checked.

| Exit code | Meaning |
| --- | --- |
| 0 | A tool was recognised. Its bare name is printed on one line, for example `pytest`. |
| 1 | No tool was recognised, so nothing is printed. Scripts such as `pnpm run test` and programs such as `make` and `git` end up here. It does not mean the command failed. |
| 2 | The arguments were wrong, for example no command was given. |

## Run a whole suite

`examples/Makefile` prints the grouped layout and has two entry points. Copy it and replace the modules.

```
== Checks ==

Module A checks:
  ✔ PASS (ruff) Format check passed; 1.8s
  ✔ PASS (ruff) Lint check passed; 0.0s
  ✔ PASS (ty) Type checks passed; 2.1s

Module B checks:
  ✔ PASS (biome) Lint and format check passed; 0.9s
  ✔ PASS (tsc) Type checks passed; 0.7s
  ✔ PASS (vite) Build passed; 0.9s

== Tests ==

Module A tests:
  ✔ PASS (pytest) Unit tests passed (2 tests); 0.9s

Module B tests:
  ✔ PASS (vitest) Unit tests passed (1 test); 0.4s
```

| Command | Who uses it | Behavior |
| --- | --- | --- |
| `make verify` | Coding agents | Stops at the first failing step and passes `--failfast` to every step. |
| `make verify-all` | People | Runs every step and reports every failure. |

Fail-fast is a Makefile choice that becomes a `--failfast` flag on each call. There is no environment variable that switches it, because environment variables are easy to lose or leak between tools.

## Tell agents which command to run

Add this to the project's `AGENTS.md` (or `CLAUDE.md`, or a skill):

```
## Verifying changes

- Run `make verify` after every change and before saying work is done. Do not run pytest, ruff, vitest, tsc, biome, eslint or prettier directly. The wrapper hides noise and keeps context small.
- If a step fails, the output lists `path:line` locations and a log path. Fix those locations first. Read the log only if the summary is not enough.
- Use `make verify-all` only when you want every failure at once.
- To run one tool or a subset, call the wrapper directly and keep the tool after `--`, for example `hush --failfast -- uv run pytest tests/test_retries.py -k duplicate`. Never run the tool bare.
```

Agents forget. For Claude Code, `examples/claude-code/` has a hook that rejects a bare test, lint or build command and tells the agent the exact `hush` command to run instead. It comes with settings for all repos or one repo, guidance snippets and tests. See [its README](examples/claude-code/README.md).

## How it finds what failed

Each supported tool has a small parser (`src/hush/parsers/`) that knows its output. It gives the best explanation: test names, Expected and Received, error codes. Supported: pytest, vitest, ruff, ty, mypy, tsc, biome, vite, eslint and prettier. `pnpm`, `uv`, `npx`, `bunx` and `python -m` are looked through to find the tool behind them.

Tools change their output between releases, so a parser is never trusted blindly:

1. A parser's failures are kept only if the files they name exist under the working directory. Anything else is thrown away.
2. If the chosen parser finds nothing valid, the other tool parsers are tried. This is what makes `pnpm run typecheck` work: the output is recognised as tsc output.
3. If no tool parser fits, a tool-agnostic extractor takes over (`src/hush/extract.py`). It looks for `path:line` references to existing files and groups the nearby lines. Which symbols or colors the tool uses does not matter.
4. If that finds no existing file either, the last 20 lines of output are shown. If the command printed nothing at all, the report says so.

So a tool release that rewords its output degrades the explanation, from precise to approximate to the raw tail. It does not turn into a wrong answer or a silent pass. A tool with no parser at all (a custom script, `make`) gets steps 3 and 4.

Colors and terminal control codes are stripped before any of this runs.

Known limits:

- The generic explanation is a best guess from nearby lines. It can include a few lines of source code or miss a detail. The full log is always saved.
- Prettier and biome format failures name the file only. They have no line number.
- A tool parser's failure whose file cannot be found is not shown. The report counts them ("and N more not matched to a file") and the log has the details.
- A parser can still be wrong in a way the file check cannot catch, for example by naming a real file with the wrong line. The tests use real captured output to guard against this, but a new tool version can break a parser until its fixture is refreshed.
- Paths are only recognised if they are relative to the directory the command ran in. A path that leads outside the directory where you started `hush` (through `..` or a symlink) is never reported as a location, even if the file exists.
- On the pass line, pytest and vitest also show `skipped`, `xfailed`, `xpassed` and `todo` counts, and pytest shows `warnings`, each only when above 0. They come from the tool's summary line, so a test that prints a fake line could change them too. Any other warnings in a passing run (from a compiler or linter, say) are hidden by design.
- The `N tests` count comes from the tool's summary line, or the last `N passed` in the output for other tools. A test that prints a fake line could change it.

## Safety

hush runs whatever command it is given, so treat it like a shell. Allowing `hush` (or a `make verify` whose Makefile you do not trust) in an agent's permission rules is the same as allowing any command.

What it does to limit the damage:

- The command runs as an argument list. There is no shell and no `eval`. Its input is closed, so a prompt cannot hang the run. `NO_COLOR=1` and `FORCE_COLOR=0` are set. `CI` is left alone, because tools such as vitest refuse to write new snapshots when it is set. The rest of your environment is passed through.
- The command gets its own process group. A timeout, Ctrl-C, SIGTERM or SIGHUP stops the command and everything it started. hush sends SIGTERM first, then SIGKILL once the command's main process has exited or after 2 seconds, whichever comes first. A second signal while it is stopping sends SIGKILL at once. The report and the log are still written. After a signal the exit code is 128 plus the number of the first signal. Windows is not supported.
- If a parent starts hush with SIGCHLD ignored, hush resets it to the default for the run and puts it back afterwards. Without that, Linux would report every command as having exited with code 0.
- The run ends when the command exits. A process the command leaves running in the background is not stopped. hush stops reading its output 1 second after the command exits.
- Output is held in memory only, capped by `--max-capture-mb`. Nothing raw is written to disk. Peak memory depends on the number of lines as well as the bytes, because cleaning, redaction and parsing each keep per-line data. Measured on macOS at the default cap:

  | Output | Peak memory |
  | --- | --- |
  | 1,000 short lines | about 27 MB |
  | 1 million short lines | about 135 MB |
  | 120,000 lines of 100 characters | about 120 MB |
  | 500 long lines holding 285,000 file references | about 100 MB |
  | 6 million one-character lines | about 600 MB |
- The log is written once, after the run, already cleaned and redacted. It goes through a private temporary file (mode 0600) that replaces the log in one step. A symlink planted at the log path is replaced, not followed. Pruning (see `--keep-logs`) deletes only regular files whose names match the `hush` log pattern, and never follows symlinks or enters subdirectories. hush does not check that `--log-dir` itself is a real directory, so do not point it somewhere an untrusted repo controls.
- Cleaning removes terminal escape codes, resolves `\r` overwrites, and removes control characters and invisible characters (zero-width, bidi controls, and the Unicode tag block that can hide text from people but not from a model). Lines are cut at 4000 characters. A line that is about to be cut is redacted first, so a long secret is not left half visible.
- Redaction replaces known token shapes (GitHub, GitLab, AWS, Google, npm, Slack, Stripe, JWTs, bearer and basic auth, private key blocks), credentials inside URLs, and the values of environment variables named like secrets (`TOKEN`, `SECRET`, `PASSWORD`, `PASS`, `PWD`, `KEY`, `AUTH`, `DSN`, `API_KEY` and so on, including their JSON and repr forms). It covers the explanation lines, the part of a location after `::` (a test id can contain anything), the log and `--verbose` output. It is a best effort, not a guarantee.
- Redaction runs in two stages. Private key blocks and environment values that contain a newline are redacted first, over the whole output, because they span lines. Everything else is redacted after parsing, so a file name that happens to match a secret is not damaged.
- The explanation lines come from the repo under test, so treat them as untrusted text. They are cut to 240 characters and 8 lines per failure.

## Develop

```
make verify
```

`make verify` also runs the unit tests on Python 3.12 in a separate environment (`.tmp/venv312`), so the project `.venv` stays on its own Python. To run only that step, use `make tests-py312`.

`tests/fixtures` holds output captured from real runs of pytest, vitest, tsc, ruff, ty, mypy, biome, eslint, prettier and vite. To support a new tool, capture a failing run into `tests/fixtures`, add a parser in `src/hush/parsers/`, register it in `PARSERS`, and add the tool name to `INNER_TOOLS` in `src/hush/tools.py`. A test checks that the two lists match. It is optional. Without one the tool still works through the generic extractor. When a tool release breaks a parser, refresh its fixture and fix the parser. The fallbacks keep the output usable in the meantime.

## License

MIT. See [LICENSE](/LICENSE).