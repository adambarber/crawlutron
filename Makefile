# crawlutron developer Makefile.
# Convenience entrypoints that mirror scripts/run-tests.sh without
# requiring zsh.

PYTHON ?= python3
SUITE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))
HOOK_CONFIG := $(SUITE_DIR)/.pre-commit-config.yaml

# The gates are declared once, in .pre-commit-config.yaml, and run from here by
# hook id. pre-commit is resolved from the service's pinned .venv first, for the
# same reason scripts/gate resolves the interpreter: a gate should run the tools
# the project pins, not whatever happens to be on PATH. Overridable, as PYTHON is.
# Declared below SUITE_DIR because `:=` expands immediately -- above it, the venv
# path would resolve against an empty root and silently fall through to PATH.
VENV_BIN := $(SUITE_DIR)/.venv/bin
PRE_COMMIT ?= $(if $(wildcard $(VENV_BIN)/pre-commit),$(VENV_BIN)/pre-commit,pre-commit)

.PHONY: help install test test-fast test-all coverage coverage-report graph lint typecheck clean

help:
	@echo "crawlutron development tasks:"
	@echo "  make install   - install package + dev dependencies"
	@echo "  make test      - run the fast hermetic test suite (default pytest)"
	@echo "  make test-fast - alias for 'make test'"
	@echo "  make test-all  - run the full suite including browser-marked tests"
	@echo "  make coverage  - run the coverage hook by id (pre-push stage; measured-descriptive)"
	@echo "  make coverage-report - show the collected coverage report (after make coverage)"
	@echo "  make lint      - run the ruff hook (the style gate)"
	@echo "  make typecheck - run the mypy hook (the fail type gate)"
	@echo "  make graph     - regenerate docs/module-graph.md"
	@echo "  make clean     - remove caches and build artifacts"

install:
	$(PYTHON) -m pip install -e ".[dev]"

test test-fast:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest

test-all:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest -o addopts=

# Coverage is opt-in and lives in .pre-commit-config.yaml as the `coverage`
# hook, `stages: [pre-push]`: a ~45s measurement on every commit would train
# --no-verify, but on pre-push it stays real without the per-commit tax.
# This target delegates by hook id rather than restating the command -- the
# Makefile is not a second declaration of any gate (the contract test
# enforces that). Measurement only: no --cov-fail-under, no threshold. Policy
# on the number is a separate owner ruling.
coverage:
	cd "$(SUITE_DIR)" && $(PRE_COMMIT) run coverage --all-files --config "$(HOOK_CONFIG)" --hook-stage pre-push

# The human-readable report, deliberately a separate step from the timed
# measurement: report generation costs real seconds that would otherwise sit
# inside the hook's wall time. Run `make coverage` first, then this.
coverage-report:
	cd "$(SUITE_DIR)" && $(PYTHON) -m coverage report --skip-covered --sort=cover

# lint and typecheck run the pre-commit hooks by ID rather than restating their
# commands. They used to spell the commands out, which made this file a second
# declaration of every gate: a third entry point to one assertion, not a third
# opinion, and a gate that lives in three places is three places to keep in step
# when a rule moves. The drift had already happened -- `ruff check app tests
# scripts` here against the hook's identical command, and a bare `mypy` here
# against the hook's `mypy app` -- and the two disagreed about both the scope
# and the interpreter, since this file ran whatever `python3` was on PATH while
# every gate resolves the service's pinned .venv through scripts/gate.
# Delegating by id deletes the duplication instead of documenting it, and the
# commands now live in exactly one file: .pre-commit-config.yaml, which CI also
# reads rather than re-declaring.
#
# tests/test_precommit_ci_contract.py is what keeps it that way: it fails if a
# target here restates a hook command, and if a target names a hook id that does
# not exist.
lint:
	cd "$(SUITE_DIR)" && $(PRE_COMMIT) run ruff --all-files --config "$(HOOK_CONFIG)"

# Pyright is the third opinion that finds annotation lies mypy cannot see -- the
# `regex` package is untyped in mypy.ini and degrades its values to Any -- and it
# is gated by the same budget test, in the two places that actually run: as part
# of the pytest suite (`make test`) and as the pre-commit `pyright` hook, which
# analyses the tree on every commit rather than on a schedule. It has no target
# here for the same reason it is not a third declaration of mypy: its budget
# already has two homes and a moving one is a fourth.
typecheck:
	cd "$(SUITE_DIR)" && $(PRE_COMMIT) run mypy --all-files --config "$(HOOK_CONFIG)"

# Module coupling report: fan-in hubs, fan-out reachers, and import cycles.
# The ranked report is the work list for the splitting programme -- a file's size
# says how much work a split is, its fan-in says how much risk. `--mermaid`
# regenerates docs/module-graph.md.
graph:
	cd "$(SUITE_DIR)" && $(PYTHON) scripts/module_graph.py
	cd "$(SUITE_DIR)" && $(PYTHON) scripts/module_graph.py --mermaid -o docs/module-graph.md

clean:
	# Remove top-level caches and patterns first; ignore-missing tolerates
	# the case where a pattern (e.g. .ruff_cache) was never created.
	rm -rf "$(SUITE_DIR)/.pytest_cache" "$(SUITE_DIR)/.ruff_cache" \
		"$(SUITE_DIR)/.coverage"
	# Walk the tree from the leaves up so nested __pycache__ dirs are deleted
	# before their parents (avoids "No such file or directory" from find when
	# ``-exec rm -rf`` invalidates a parent's path mid-run).
	find "$(SUITE_DIR)" -type d -name __pycache__ -prune -exec rm -rf {} +
	find "$(SUITE_DIR)" -type d -name "*.egg-info" -prune -exec rm -rf {} +
