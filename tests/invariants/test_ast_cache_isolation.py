"""Red-first test: AST cache returns isolated copies with metadata.

Each caller receives an independent copy of the _parsed_copy AST carrying version +
validation metadata (source SHA-256, AST schema version, validation result).
"""

import ast
from pathlib import Path

from tests._walk_primitives import _PARSE_CACHE, _parsed_copy
from tests.chokepoint_paths import REPO_ROOT


def test_ast_cache_returns_isolated_copies_with_metadata():
    """Each call to _parsed_copy() for the same file must return a distinct object
    carrying metadata, and mutating one must not affect the other.
    """
    # Pick a stable test file
    test_file = REPO_ROOT / "tests" / "_walk_primitives.py"

    # Parse twice - should get two independent copies
    tree1 = _parsed_copy(test_file)
    tree2 = _parsed_copy(test_file)

    # They must be different objects (isolated copies)
    assert tree1 is not tree2, "_parsed_copy() must return isolated copies, not shared object"

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

    tree1 = _parsed_copy(test_file)
    tree2 = _parsed_copy(test_file)

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


def test_ast_cache_canonical_tree_stored_once(tmp_path: Path) -> None:
    """A fresh source is stored once; the shared cache is never evicted."""
    test_file = tmp_path / "unique_module.py"
    test_file.write_text("value = 1\n", encoding="utf-8")
    before = len(_PARSE_CACHE)

    _parsed_copy(test_file)
    after_first = len(_PARSE_CACHE)
    _parsed_copy(test_file)

    assert after_first == before + 1
    assert len(_PARSE_CACHE) == after_first


def test_ast_cache_different_sources_different_entries(tmp_path: Path) -> None:
    """Two fresh sources add two entries without clearing the shared cache."""
    first = tmp_path / "one.py"
    second = tmp_path / "two.py"
    first.write_text("a = 1\n", encoding="utf-8")
    second.write_text("b = 2\n", encoding="utf-8")
    before = len(_PARSE_CACHE)

    _parsed_copy(first)
    _parsed_copy(second)

    assert len(_PARSE_CACHE) == before + 2
