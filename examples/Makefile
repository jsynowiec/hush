# Example suite for hush. Copy it and replace the modules and commands.
#
#   make verify       Agent path. Stops at the first failing step and asks pytest and
#                     vitest to stop at their first failing test.
#   make verify-all   Human path. Runs every step and reports every failure.
#
# Make prints "*** Error" lines when a step fails. That is Make, not hush.

RS ?= hush
FAILFAST ?= 0
# Set VERBOSE=1 to also print the cleaned tool output to stderr after each step.
# COLOR=always colours the pass and fail marks when Make's output is not a terminal.
COLOR ?= auto
FLAGS := --color $(COLOR) $(if $(filter 1,$(FAILFAST)),--failfast) $(if $(filter 1,$(VERBOSE)),--verbose)
# Remember a failed step. In failfast mode also stop the target right away.
ON_FAIL := || { rc=1; $(if $(filter 1,$(FAILFAST)),exit 1;) }

# $(call step,<module dir>) is the hush prefix for a step run inside that module.
# Logs go to .tmp/test-results in the repo root. Paths are reported from the repo root.
step = $(RS) $(FLAGS) --cwd $(1) --log-dir .tmp/test-results --timeout 600

.PHONY: verify verify-all checks tests

verify:
	@$(MAKE) -s FAILFAST=1 checks tests

verify-all:
	@$(MAKE) -s -k checks tests

checks:
	@rc=0; \
	printf '\n== Checks ==\n\n'; \
	printf 'Module A checks:\n'; \
	$(call step,module-a) --name "Format check" -- uv run ruff format --check . $(ON_FAIL); \
	$(call step,module-a) --name "Lint check" -- uv run ruff check . $(ON_FAIL); \
	$(call step,module-a) --name "Type checks" -- uv run ty check $(ON_FAIL); \
	printf '\nModule B checks:\n'; \
	$(call step,module-b) --name "Lint and format check" -- pnpm exec biome check src $(ON_FAIL); \
	$(call step,module-b) --name "Type checks" -- pnpm exec tsc --noEmit $(ON_FAIL); \
	$(call step,module-b) --name "Build" -- pnpm exec vite build $(ON_FAIL); \
	exit $$rc

tests:
	@rc=0; \
	printf '\n== Tests ==\n\n'; \
	printf 'Module A tests:\n'; \
	$(call step,module-a) --name "Unit tests" -- uv run pytest $(ON_FAIL); \
	printf '\nModule B tests:\n'; \
	$(call step,module-b) --name "Unit tests" -- pnpm exec vitest run $(ON_FAIL); \
	exit $$rc
