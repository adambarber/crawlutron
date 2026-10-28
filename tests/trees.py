"""Shared tree-scope helpers the invariant modules keep reaching for.

Declared support surface (#476): a sibling test module must import these from
here, not from another test module's internals.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import APP_ROOT, TESTS_DIR, trees_under

__all__ = ["tree_under_tests", "app_reads", "test_tree_module_names"]


def tree_under_tests(*, exclude: frozenset[str]) -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    """The test tree, swept whole.

    A drop-in for ``app_trees`` that ignores ``exclude``; exemptions are named
    against ``app/`` modules, not a ``tests/`` list.
    """
    return trees_under(TESTS_DIR)


def app_reads(monkeypatch) -> list[pathlib.Path]:
    """Record every `read_text` of a file under app/ for the rest of this test."""
    original = pathlib.Path.read_text
    seen: list[pathlib.Path] = []

    def counting(self: pathlib.Path, *args, **kwargs) -> str:
        if self.is_relative_to(APP_ROOT):
            seen.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(pathlib.Path, "read_text", counting)
    return seen


def test_tree_module_names() -> frozenset[str]:
    """The bare module names that name a file in the test tree.

    Cached because the sweep asks once per import and the set does not change
    within a run -- what keeps this affordable at every call site."""
    return frozenset(candidate.stem for candidate in TESTS_DIR.rglob("*.py"))
