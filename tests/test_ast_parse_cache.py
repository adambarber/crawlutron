"""S6 contract: parsed() shares one canonical AST per (path, content) per session and returns
isolated copies with metadata. Re-parses on any content change -- even a rewrite that keeps
size and mtime identical (the same-size-same-mtime stale hole).
"""

import ast
import os

from tests._walk_primitives import parsed
from tests.invariants._clock_family import single_target_assigns


def _assigned_constant(tree: ast.Module, name: str) -> object:
    """The value of the first top-level literal assignment to `name`."""
    for target, node in single_target_assigns(tree):
        if target == name and isinstance(node.value, ast.Constant):
            return node.value.value
    raise AssertionError(f"no literal assignment to {name} in parsed tree")


def test_parsed_returns_isolated_copies_with_identical_metadata_and_structure(tmp_path) -> None:
    """Repeat reads of unchanged content yield isolated copies with identical
    cache metadata and structural equality (ast.dump). Object identity is NOT
    guaranteed -- each caller receives an independent copy.
    """
    path = tmp_path / "share.py"
    path.write_text("SHARED = 1\n", encoding="utf-8")
    tree1 = parsed(path)
    tree2 = parsed(path)
    # Must be different objects (isolated copies)
    assert tree1 is not tree2, "parsed() must return isolated copies, not shared object"
    # Both must carry cache metadata
    assert hasattr(tree1, "_cache_metadata"), "tree1 missing _cache_metadata"
    assert hasattr(tree2, "_cache_metadata"), "tree2 missing _cache_metadata"
    # Metadata must be identical (same canonical source)
    assert tree1._cache_metadata == tree2._cache_metadata, "metadata mismatch for same source"
    # Structural equality via ast.dump
    assert ast.dump(tree1) == ast.dump(tree2), "copies must be structurally identical"


def test_same_size_same_mtime_rewrite_is_reparsed(tmp_path) -> None:
    """A rewrite preserving both size and mtime is still re-parsed from fresh source.

    Keying on (mtime_ns, size) cannot identify content; keying on the content digest can.
    """
    path = tmp_path / "rewrite.py"
    path.write_text("BODY = 10\n", encoding="utf-8")  # ten bytes
    stamp = path.stat()
    assert _assigned_constant(parsed(path), "BODY") == 10

    path.write_text("BODY = 20\n", encoding="utf-8")  # still ten bytes
    os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns))
    assert path.stat().st_size == stamp.st_size
    assert path.stat().st_mtime_ns == stamp.st_mtime_ns

    value = _assigned_constant(parsed(path), "BODY")
    assert value == 20, f"stale source served after same-size-same-mtime rewrite: got {value}"


def test_parsed_copies_are_mutation_isolated(tmp_path) -> None:
    """Mutating one caller's copy must not affect another caller's copy."""
    path = tmp_path / "mutate.py"
    path.write_text("X = 1\nY = 2\n", encoding="utf-8")
    tree1 = parsed(path)
    tree2 = parsed(path)
    initial_dump2 = ast.dump(tree2)
    # Mutate tree1
    bogus_node = ast.Expr(value=ast.Constant(value="MUTATED", lineno=99999))
    tree1.body.append(bogus_node)
    # tree2 must be unchanged
    assert ast.dump(tree2) == initial_dump2, "tree2 was corrupted by tree1's mutation"
    # tree1 must show the mutation
    assert ast.dump(tree1) != initial_dump2, "tree1 mutation not reflected"
