# Checks and tests for hush itself, run through hush.
#   make verify       Agent path: stops at the first failing step.
#   make verify-all   Human path: runs every step.

RS ?= uv run hush
FAILFAST ?= 0
COLOR ?= auto
FLAGS := --color $(COLOR) $(if $(filter 1,$(FAILFAST)),--failfast) $(if $(filter 1,$(VERBOSE)),--verbose)
ON_FAIL := || { rc=1; $(if $(filter 1,$(FAILFAST)),exit 1;) }

.PHONY: verify verify-all checks tests tests-py312

verify:
	@$(MAKE) -s FAILFAST=1 checks tests tests-py312

verify-all:
	@$(MAKE) -s -k checks tests tests-py312

checks:
	@rc=0; \
	printf '\n== Checks ==\n\n'; \
	$(RS) $(FLAGS) --name "Format check" -- uv run ruff format --check . $(ON_FAIL); \
	$(RS) $(FLAGS) --name "Lint check" -- uv run ruff check . $(ON_FAIL); \
	$(RS) $(FLAGS) --name "Type checks" -- uv run ty check $(ON_FAIL); \
	exit $$rc

tests:
	@rc=0; \
	printf '\n== Tests ==\n\n'; \
	$(RS) $(FLAGS) --name "Unit tests" -- uv run pytest $(ON_FAIL); \
	exit $$rc

# A separate environment on Python 3.12, so the project .venv is untouched. Both variables are set
# per target and not in the command, so hush still sees `uv run pytest` and labels it (pytest).
# They also apply to the `uv run hush` that starts the step, so it runs on 3.12 too.
tests-py312: export UV_PROJECT_ENVIRONMENT := .tmp/venv312
tests-py312: export UV_PYTHON := 3.12
tests-py312:
	@rc=0; \
	$(RS) $(FLAGS) --name "Unit tests (py3.12)" -- uv run pytest $(ON_FAIL); \
	exit $$rc
