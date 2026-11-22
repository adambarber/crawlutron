"""The suite's memoized walk helpers serve repeat asks from one cached result."""

from __future__ import annotations

from pathlib import Path

from tests._walk_primitives import import_roots, package_roots, trees_under
from tests._walk_trees import app_trees
from tests.invariants._cache_walk_probe import assert_repeat_call_skips_the_walk
from tests.trees import test_tree_module_names as _test_tree_module_names


def test_package_roots_returns_same_object_twice():
    """Two calls to package_roots() must return the exact same object."""
    assert package_roots() is package_roots()


def test_import_roots_is_memoized_without_clearing_the_cache():
    """A repeat ask reuses the first derivation; no test invalidates it for others."""
    assert import_roots() is import_roots()


def test_app_trees_is_memoized_without_clearing_the_cache():
    """The metric collectors share one app-tree result across every ask."""
    assert app_trees() is app_trees()


def test_trees_under_walks_a_fresh_directory_once(tmp_path: Path) -> None:
    """A cold, test-owned directory proves one rglob for two asks, without evicting a live cache."""
    (tmp_path / "sample.py").write_text("value = 1\n", encoding="utf-8")
    trees = assert_repeat_call_skips_the_walk(lambda: trees_under(tmp_path))
    assert len(trees) == 1


def test_tree_module_names_is_memoized_without_clearing_the_cache():
    """The test-tree name set is one object for the whole process."""
    assert _test_tree_module_names() is _test_tree_module_names()
