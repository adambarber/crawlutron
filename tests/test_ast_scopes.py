"""The scope vocabulary in scripts/ast_scopes.py is the ONLY declaration."""

from __future__ import annotations

import ast
from pathlib import Path

from scripts.ast_scopes import SCOPE_OPENING_NODES
from tests.chokepoint_walks import REPO_ROOT

# Consumers of the vocabulary; the declaration file is exempt (the tuple there
# IS the declaration).
_CONSUMERS = (
    "scripts/jscpd_sweep.py",
    "tests/test_metrics_budget.py",
    "tests/invariants/test_knob_contracts.py",
)

_VOCABULARY_NAMES = {node.__name__ for node in SCOPE_OPENING_NODES}


def _ast_type_tuple_members(path: Path) -> list[tuple[int, str]]:
    """Every ``ast.<Name>`` appearing inside a tuple of node types, as (line, name)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        for element in node.elts:
            if (
                isinstance(element, ast.Attribute)
                and isinstance(element.value, ast.Name)
                and element.value.id == "ast"
            ):
                found.append((element.lineno, element.attr))
    return found


def test_no_consumer_restates_a_shared_vocabulary_member() -> None:
    """A consumer tuple that re-adds a node the shared vocabulary already holds
    is a second declaration -- the one thing the module docstring forbids."""
    restatements = []
    for name in _CONSUMERS:
        for lineno, member in _ast_type_tuple_members(REPO_ROOT / name):
            if member in _VOCABULARY_NAMES:
                restatements.append(f"{name}:{lineno}: ast.{member} is in SCOPE_OPENING_NODES")
    assert not restatements, (
        "the scope vocabulary was restated at a consumer:\n  "
        + "\n  ".join(restatements)
        + "\nImport the vocabulary instead; a second list that happens to agree is "
        "a list that can stop agreeing."
    )


def test_except_handler_opens_a_scope_and_match_does_not() -> None:
    """An `except` handler owns a real `.body` suite: scope-opening. `ast.Match`
    is deliberately NOT -- it owns `.cases`, never `.body`, so body walks would
    break on it, and the metrics budgets are measured without it."""
    assert ast.ExceptHandler in SCOPE_OPENING_NODES
    assert ast.Match not in SCOPE_OPENING_NODES
    assert ast.TryStar in SCOPE_OPENING_NODES
