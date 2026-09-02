# browser-crawler developer Makefile.
# Convenience entrypoints that mirror scripts/run-tests.sh without
# requiring zsh.

PYTHON ?= python3
SUITE_DIR := $(abspath $(dir $(lastword $(MAKEFILE_LIST))))

.PHONY: help install test test-fast test-all lint clean

help:
	@echo "browser-crawler development tasks:"
	@echo "  make install   - install package + dev dependencies"
	@echo "  make test      - run the fast hermetic test suite (default pytest)"
	@echo "  make test-fast - alias for 'make test'"
	@echo "  make test-all  - run the full suite including browser-marked tests"
	@echo "  make lint      - run ruff on the app/ and tests/ trees"
	@echo "  make clean     - remove caches and build artifacts"

install:
	$(PYTHON) -m pip install -e ".[dev]"

test test-fast:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest

test-all:
	cd "$(SUITE_DIR)" && $(PYTHON) -m pytest -o addopts=

lint:
	cd "$(SUITE_DIR)" && $(PYTHON) -m ruff check app tests

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
