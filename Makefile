# browser-crawler developer Makefile.
# Convenience entrypoints that mirror scripts/run-tests.sh without
# requiring zsh.

PYTHON ?= python3
SUITE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: help install test test-fast test-all lint typecheck clean

help:
	@echo "browser-crawler development tasks:"
	@echo "  make install   - install package + dev dependencies"
	@echo "  make test      - run the fast hermetic test suite (default pytest)"
	@echo "  make test-fast - alias for 'make test'"
	@echo "  make test-all  - run the full suite including browser-marked tests"
	@echo "  make lint      - run ruff on the app/ and tests/ trees"
	@echo "  make typecheck - run mypy on the app tree (required CI gate)"
	@echo "  make clean     - remove caches and build artifacts"

install:
	$(PYTHON) -m pip install -e ".[dev]"

test test-fast:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest

test-all:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest -o addopts=

lint:
	cd "$(SUITE_DIR)" && $(PYTHON) -m ruff check app tests scripts

# Type-check with mypy (the fail gate). Pyright is the third opinion that finds
# annotation lies mypy cannot see -- the `regex` package is untyped in mypy.ini and
# degrades its values to Any -- and it is gated by the same budget test, in the
# two places that actually run: as part of the pytest suite (`make test`) and as
# the pre-commit `enforcement` hook, which analyses the tree on every commit
# rather than on a schedule.
#
# It used to be invoked here a third time. That was a third entry point to one
# assertion, not a third opinion, and a gate that lives in three places is three
# places to keep in step when the budget moves. This target is mypy because that
# is the only one of the two with no automated home.
typecheck:
	cd "$(SUITE_DIR)" && $(PYTHON) -m mypy

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
