"""S6 contract: parsed() shares one AST per (path, content) per session and re-parses on any content
change -- even a rewrite that keeps size and mtime identical (the same-size-same-mtime stale hole).
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


def test_parsed_returns_the_same_tree_object_on_repeat_reads(tmp_path) -> None:
    """Repeat reads of unchanged content share one AST object."""
    path = tmp_path / "share.py"
    path.write_text("SHARED = 1\n", encoding="utf-8")
    assert parsed(path) is parsed(path)


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
