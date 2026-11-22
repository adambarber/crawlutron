"""suite_trees() yields AST copies with identical metadata and structure via parsed() (#1098)."""

import ast

from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import trees_under
from tests.invariants._clock_family import suite_trees


def test_suite_trees_same_metadata_and_structure_as_trees_under():
    """Every path in suite_trees() yields a copy with identical metadata and
    structural equality (ast.dump) to trees_under(). Object identity NOT required.
    """
    suite_map = {path: tree for path, tree in suite_trees()}
    trees_map = {path: tree for path, tree in trees_under(REPO_ROOT / "tests")}
    for path in suite_map:
        assert path in trees_map, f"missing from trees_under: {path}"
        suite_tree = suite_map[path]
        trees_tree = trees_map[path]
        # Both must carry cache metadata
        assert hasattr(suite_tree, "_cache_metadata"), f"suite_trees tree for {path} missing _cache_metadata"
        assert hasattr(trees_tree, "_cache_metadata"), f"trees_under tree for {path} missing _cache_metadata"
        # Metadata must be identical (same canonical source)
        assert suite_tree._cache_metadata == trees_tree._cache_metadata, (
            f"metadata mismatch for {path}: "
            f"suite_trees={suite_tree._cache_metadata}, "
            f"trees_under={trees_tree._cache_metadata}"
        )
        # Structural equality via ast.dump
        assert ast.dump(suite_tree) == ast.dump(trees_tree), f"structural mismatch for {path}"
        # Object identity must NOT hold (isolated copies)
        assert suite_tree is not trees_tree, (
            f"identity unexpectedly shared for {path}: each caller must receive an isolated copy"
        )
