"""Every python-bearing source parses under the 3.12 grammar floor.

Unparenthesized multi-exception ``except`` clauses are a 3.14-only shape.
"""

from __future__ import annotations

import ast

import pytest

from tests._walk_primitives import python_bearing_roots
from utils.paths import REPO_ROOT

_ROOTS = python_bearing_roots()


def _source_files():
    """Yield every derived python-bearing source file, skipping bytecode caches."""
    for root in _ROOTS:
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path


def test_every_source_parses_at_the_312_floor() -> None:
    """A 3.14-only syntax shape must not appear in any scanned source file."""
    failures: list[str] = []
    for path in _source_files():
        source = path.read_text(encoding="utf-8")
        try:
            ast.parse(source, feature_version=(3, 12))
        except SyntaxError as exc:
            label = path.relative_to(REPO_ROOT).as_posix()
            failures.append(f"{label}:{exc.lineno} {exc.msg}")
    assert not failures, "3.12 parse failures:\n" + "\n".join(failures)


def test_probe_distinguishes_the_offending_shape() -> None:
    """The 3.12 grammar rejects bare exception tuples but accepts parenthesized ones."""
    parenthesized = "try:\n pass\nexcept (ValueError, TypeError):\n pass\n"
    bare = "try:\n pass\nexcept ValueError, TypeError:\n pass\n"
    ast.parse(parenthesized, feature_version=(3, 12))
    with pytest.raises(SyntaxError):
        ast.parse(bare, feature_version=(3, 12))
