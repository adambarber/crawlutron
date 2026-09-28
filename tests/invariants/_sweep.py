"""The tree-wide sweep every invariant check in :mod:`tests.invariants` shares.

This file exists because the invariant checks used to live in one 9.5k-line
module, and that module is now split by theme into this package. ``assert_trees_clean``
is the one piece every one of those checks needs, and it is here rather than
duplicated per themed module so that "sweep the whole app tree" has exactly one
definition to drift from.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterable
from pathlib import Path

from tests.chokepoint_walks import app_trees

__all__ = ["assert_trees_clean"]


def assert_trees_clean(
    detector: Callable[[ast.AST, Path], list[str]],
    message: str,
    *,
    trees: Iterable[tuple[Path, ast.AST]] | None = None,
) -> None:
    """Sweep every module through `detector` and fail on the offenders it finds.

    This is the tree-wide walk every invariant check depends on, so it is written
    once here rather than per detector. It used to be four lines copied into each
    of ~60 tests -- declare the list, walk ``app_trees()``, ``extend``, assert --
    and a copy is exactly what makes a check that sweeps nothing, or sweeps a
    hard-coded file list, look identical to one that sweeps the tree the rule
    actually requires (see :func:`tests.chokepoint_walks.app_trees`).

    ``trees`` defaults to the whole app tree; a check passes an explicit walker
    only to exclude sanctioned modules, and the default is what makes a forgotten
    sweep fail loudly rather than silently pass on an empty list.
    """
    offenders = [
        hit
        for path, tree in (app_trees() if trees is None else trees)
        for hit in detector(tree, path)
    ]
    assert not offenders, f"{message}: {', '.join(offenders)}"
