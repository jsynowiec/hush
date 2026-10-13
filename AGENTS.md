# Working guidelines

- Run `make verify` before saying a change is done. It runs format, lint, type checks and tests through `hush` itself.
- Tool-specific output parsing lives only in `src/hush/parsers/`. Each parser is optional polish. Never make correctness depend on one: the pipeline in `parsers/__init__.py` must keep checking that reported files exist, trying the other parsers, and falling back to `src/hush/extract.py` and the output tail.
- Outside `examples/`, tool names appear only in `src/hush/tools.py`, for the label, the fail-fast flag, the arguments hush masks (such as `docker exec -e`), and the container working directory (`docker exec -w`). `examples/claude-code/` may name tools and tool arguments, because it decides which invocations are routine. Keep that knowledge out of `src/`.
- `examples/claude-code/hooks/hush_nudge.py` runs on the system `python3` (3.9 on macOS), and ruff targets 3.12 so it will not catch newer syntax: no `match`, no `X | Y` types, no `zip(strict=)`.
- Test parsers against real captured output in `tests/fixtures`, not hand-written imitations. State expected locations by hand. Do not compute them with the code under test.
- Do not build the command into a shell string. Keep commands as argument lists.
- Keep `README.md` and `examples/Makefile` in sync with the options in `src/hush/cli.py`.
