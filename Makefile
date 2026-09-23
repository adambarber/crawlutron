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
	cd "$(SUITE_DIR)" && $(PYTHON) -m ruff check app tests

# Type-check the app tree with mypy. The config lives in mypy.ini (files = app).
# This started as an advisory target against a never-typechecked tree; the
# backlog it warned about is cleared (0 errors across 85 files) and CI now runs
# this as part of the required quality gate, so a type error fails a build.
typecheck:
	cd "$(SUITE_DIR)" && $(PYTHON) -m mypy

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
