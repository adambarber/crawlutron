"""Executable safety invariants, split by what each one is about.

These checks are the round-4 review's central lesson made mechanical: static
rejection of "pathological" shapes is bypassable, and chokepoints only hold if
bypassing them fails CI instead of code review. The AST walks here verify the
UNSAFE patterns are absent from ``app/`` so reintroducing them turns the build
red.

The modules are themed, not alphabetical, so a reviewer looking for "how is the
response envelope kept single-shaped?" reads one file rather than grepping 9.5k
lines. :mod:`tests.invariants._sweep` holds the one helper they all share.
"""

from __future__ import annotations

from tests.invariants._sweep import assert_trees_clean

__all__ = ["assert_trees_clean"]
