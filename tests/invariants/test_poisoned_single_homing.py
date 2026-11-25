"""Every write to the poisoned flag is single-homed to a production read."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_paths import APP_ROOT
from tests.chokepoint_walks import offender_at, trees_under
from tests.rules import Bites, Registry, Rule

_STORAGE_DIR = APP_ROOT / "storage"
_POISONED = "poisoned"


def _poisoned_write_without_read_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Flag a module that writes ``.poisoned`` but never reads it in production code."""
    write_node: ast.Attribute | None = None
    reads = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr != _POISONED:
            continue
        if isinstance(node.ctx, ast.Store):
            write_node = node
        elif isinstance(node.ctx, ast.Load):
            reads += 1
    if write_node is not None and not reads:
        return [offender_at(path, write_node, "writes .poisoned but never reads it (single-homing)")]
    return []


POISONED_SINGLE_HOMING = Rule(
    name="poisoned-flag-single-homed",
    find=_poisoned_write_without_read_offenders,
    summary="every production write to state.poisoned must be matched by a production read",
    scope=lambda *, exclude: (
        (path, tree) for path, tree in trees_under(_STORAGE_DIR) if path.name != "migrations"
    ),
    bite=lambda: Bites(
        "state.poisoned = True\n",
        "writes .poisoned but never reads it (single-homing)",
    ),
    clean=lambda: ("if state.poisoned:\n    raise StoreConnectionPoisoned(...)",),
)


RULES = Registry((POISONED_SINGLE_HOMING,))


def test_poisoned_flag_is_single_homed() -> None:
    """No production module writes .poisoned without reading it."""
    RULES.check_all_clean()


def test_poisoned_flag_rule_has_working_bite() -> None:
    """The detector rejects a write-without-read shape."""
    RULES.check_all_bite()
