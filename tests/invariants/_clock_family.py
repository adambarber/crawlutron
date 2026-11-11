"""Clock-family primitives shared by the #843 invariant detectors.

The wall-member set and the tests-tree walk are the two halves both sweeps
read; one home keeps the detectors from drifting apart (the #649 law).
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Iterator

from tests.chokepoint_walks import REPO_ROOT

__all__ = [
    "CLOCK_HOME",
    "WALL_MEMBERS",
    "from_imports",
    "from_time_imports",
    "single_target_assigns",
    "suite_trees",
    "wall_names",
    "wall_read",
]

CLOCK_HOME = REPO_ROOT / "tests" / "_clocks.py"

WALL_MEMBERS = frozenset({"perf_counter", "perf_counter_ns", "time", "time_ns"})


def wall_names(tree: ast.Module) -> set[str]:
    """Bare names bound to a wall-clock member (``from time import perf_counter``)."""
    return from_time_imports(tree, WALL_MEMBERS)


def from_imports(
    tree: ast.Module, modules: frozenset[str], members: frozenset[str] | None = None
) -> set[str]:
    """Local names bound via ``from <module> import <member> [as x]`` at any scope.

    ``members`` None means every member of the module binds (re-export walks)."""
    return {
        alias.asname or alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module in modules
        for alias in node.names
        if members is None or alias.name in members
    }


def from_time_imports(tree: ast.Module, members: frozenset[str]) -> set[str]:
    """Local names bound via ``from time import <member> [as x]`` at any scope."""
    return from_imports(tree, frozenset({"time"}), members)


def wall_read(node: ast.expr, wall: set[str]) -> bool:
    """Whether ``node`` reads a wall clock: ``time.<member>()`` or a bare alias."""
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            return func.value.id == "time" and func.attr in WALL_MEMBERS
        return isinstance(func, ast.Name) and func.id in wall
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and (node.value.id == "time" and node.attr in WALL_MEMBERS)
    )


def suite_trees() -> Iterator[tuple[pathlib.Path, ast.Module]]:
    """Every ``tests/**`` module, walked once (the sweeps' shared population)."""
    for path in sorted((REPO_ROOT / "tests").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text())


def single_target_assigns(tree: ast.Module) -> Iterator[tuple[str, ast.Assign]]:
    """Every module-level ``X = <value>`` with one Name target."""
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            yield node.targets[0].id, node
