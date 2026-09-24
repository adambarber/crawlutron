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

# Type-check with mypy (the fail gate) and pyright (the third opinion that finds
# annotation lies mypy cannot see, because the `regex` package is untyped in
# mypy.ini and degrades its values to Any). Pyright analyses 101 files in under
# three seconds, so it runs on every commit rather than on a schedule.
typecheck:
	cd "$(SUITE_DIR)" && $(PYTHON) -m mypy
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest tests/test_pyright_budget.py -q -p no:randomly

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
