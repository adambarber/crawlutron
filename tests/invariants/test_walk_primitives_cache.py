"""The suite's memoized walk helpers are @cache-wrapped (#1096, #1108)."""

import tests._walk_primitives as walk_primitives
import tests._walk_trees as walk_trees
from tests._walk_primitives import import_roots, package_roots, trees_under
from tests._walk_trees import app_trees
from tests.chokepoint_paths import REPO_ROOT
from tests.invariants._cache_walk_probe import assert_repeat_call_skips_the_walk
from tests.trees import test_tree_module_names as _test_tree_module_names


def test_package_roots_returns_same_object_twice():
    """Two calls to package_roots() must return the exact same object."""
    a = package_roots()
    b = package_roots()
    assert a is b, "package_roots() must be @cache-wrapped (different tuple objects returned)"


def test_import_roots_walks_the_tree_once():
    """A repeat call costs zero rglob: the declared cache serves it."""
    import_roots.cache_clear()
    package_roots.cache_clear()
    roots = assert_repeat_call_skips_the_walk(import_roots)
    assert roots, "import_roots() resolved no import roots"


def test_app_trees_walks_the_app_once():
    """The metric collectors share one app-tree parse across every ask."""
    walk_trees._app_files.cache_clear()
    walk_trees._app_trees.cache_clear()
    trees = assert_repeat_call_skips_the_walk(app_trees)
    assert trees, "app_trees() parsed no modules"


def test_trees_under_walks_a_directory_once():
    """Repeat metric walks over one directory reuse the same cached parse."""
    walk_primitives._trees_under.cache_clear()
    trees = assert_repeat_call_skips_the_walk(lambda: trees_under(REPO_ROOT / "tests"))
    assert trees, "trees_under() parsed no modules"


def test_tree_module_names_walks_the_test_tree_once():
    """A repeat call costs zero rglob: the sweep asks once per imported module."""
    _test_tree_module_names.cache_clear()
    names = assert_repeat_call_skips_the_walk(_test_tree_module_names)
    assert "trees" in names, "the name set does not name the test support module itself"
