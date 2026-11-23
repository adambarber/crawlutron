"""Every error door's production tree for the cross-door invariant gates."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests._walk_primitives import trees_under
from tests._walk_trees import app_trees
from utils.paths import SDK_ROOT


def error_door_trees(*, exclude: frozenset[str] = frozenset()) -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Core/admit/REST+CLI/MCP/safe-url plus the SDK tree (derived from uv members)."""
    yield from app_trees(exclude=exclude)
    for path, tree in trees_under(SDK_ROOT):
        if path.name not in exclude:
            yield path, tree
