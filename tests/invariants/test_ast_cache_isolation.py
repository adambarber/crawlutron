"""Red-first test: AST cache returns isolated copies with metadata.

Each caller receives an independent copy of the parsed AST carrying version +
validation metadata (source SHA-256, AST schema version, validation result).
"""

import ast

from tests._walk_primitives import _PARSE_CACHE, parsed
from tests.chokepoint_paths import REPO_ROOT


def test_ast_cache_returns_isolated_copies_with_metadata():
    """Each call to parsed() for the same file must return a distinct object
    carrying metadata, and mutating one must not affect the other.
    """
    # Pick a stable test file
    test_file = REPO_ROOT / "tests" / "_walk_primitives.py"

    # Parse twice - should get two independent copies
    tree1 = parsed(test_file)
    tree2 = parsed(test_file)

    # They must be different objects (isolated copies)
    assert tree1 is not tree2, "parsed() must return isolated copies, not shared object"

    # Both must be ast.Module instances
    assert isinstance(tree1, ast.Module)
    assert isinstance(tree2, ast.Module)

    # Both must carry metadata attributes
    assert hasattr(tree1, "_cache_metadata"), "tree1 missing _cache_metadata"
    assert hasattr(tree2, "_cache_metadata"), "tree2 missing _cache_metadata"

    # Metadata must include source_sha256, ast_schema_version, validation_result
    meta1 = tree1._cache_metadata
    meta2 = tree2._cache_metadata

    assert "source_sha256" in meta1, "metadata missing source_sha256"
    assert "ast_schema_version" in meta1, "metadata missing ast_schema_version"
    assert "validation_result" in meta1, "metadata missing validation_result"

    # Metadata must be equal for same source (same canonical tree)
    assert meta1["source_sha256"] == meta2["source_sha256"]
    assert meta1["ast_schema_version"] == meta2["ast_schema_version"]
    assert meta1["validation_result"] == meta2["validation_result"]

    # Structural equality via ast.dump
    assert ast.dump(tree1) == ast.dump(tree2), "copies must be structurally identical"


def test_ast_cache_mutation_isolation():
    """Mutating one caller's tree must not affect another caller's tree."""
    test_file = REPO_ROOT / "tests" / "_walk_primitives.py"

    tree1 = parsed(test_file)
    tree2 = parsed(test_file)

    # Record initial structure
    initial_dump1 = ast.dump(tree1)
    initial_dump2 = ast.dump(tree2)

    # Mutate tree1 by adding a bogus node (this should not affect tree2)
    # We add a constant expression to the body - this is a structural mutation
    bogus_node = ast.Expr(value=ast.Constant(value="MUTATED", lineno=99999))
    tree1.body.append(bogus_node)

    # tree2 must be unchanged
    assert ast.dump(tree2) == initial_dump2, "tree2 was corrupted by tree1's mutation"

    # tree1 must show the mutation
    assert ast.dump(tree1) != initial_dump1, "tree1 mutation not reflected"


def test_ast_cache_canonical_tree_stored_once():
    """The canonical tree is stored once per (path, source_digest) in _PARSE_CACHE."""
    test_file = REPO_ROOT / "tests" / "_walk_primitives.py"

    # Clear cache to start fresh
    _PARSE_CACHE.clear()

    # First parse populates cache
    parsed(test_file)
    cache_size_after_first = len(_PARSE_CACHE)

    # Second parse should NOT add to cache (same source digest)
    parsed(test_file)
    cache_size_after_second = len(_PARSE_CACHE)

    assert cache_size_after_first == 1, "cache should have 1 entry after first parse"
    assert cache_size_after_second == 1, "cache should still have 1 entry after second parse (same source)"


def test_ast_cache_different_sources_different_entries():
    """Different source files (or same file with different content) get different cache entries."""
    test_file1 = REPO_ROOT / "tests" / "_walk_primitives.py"
    test_file2 = REPO_ROOT / "tests" / "chokepoint_walks.py"

    _PARSE_CACHE.clear()

    parsed(test_file1)
    parsed(test_file2)

    assert len(_PARSE_CACHE) == 2, "different files should have separate cache entries"
