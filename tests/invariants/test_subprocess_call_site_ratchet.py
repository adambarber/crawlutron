"""Subprocess call sites are a down-only ratchet: new process bombs cannot land."""

from __future__ import annotations

import ast

from tests._walk_primitives import python_bearing_roots
from utils.paths import REPO_ROOT

_CALL_NAMES = frozenset({"run", "Popen", "call", "check_output", "check_call"})
_ROOTS = python_bearing_roots()


def _sites(source: str) -> list[ast.Call]:
    """Return subprocess call nodes in one module source."""
    tree = ast.parse(source)
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
        and node.func.attr in _CALL_NAMES
    ]


def _modules():
    """Yield source text for every derived python-bearing module."""
    for root in _ROOTS:
        for path in (REPO_ROOT / root).rglob("*.py"):
            try:
                yield path.read_text(errors="replace")
            except OSError:
                continue


def test_call_site_counts_never_grow() -> None:
    """A new subprocess call site must be paid for by deleting an old one."""
    assert sum(len(_sites(source)) for source in _modules()) <= 49


def test_call_sites_inside_loops_never_grow() -> None:
    """A per-iteration process spawn is the bomb shape; its count is pinned."""
    total = 0
    for source in _modules():
        tree = ast.parse(source)
        calls = {node.lineno for node in _sites(source)}
        in_loops: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.For, ast.AsyncFor, ast.While)):
                for child in ast.walk(node):
                    if isinstance(child, ast.Call):
                        in_loops.add(child.lineno)
        total += len(calls & in_loops)
    assert total <= 3


def test_ratchet_bites_an_injected_call_site() -> None:
    """An injected subprocess call is detected by the same AST predicate."""
    assert _sites("def f(): pass\n") == []
    assert len(_sites("import subprocess\ndef f(): subprocess.run(['x'])\n")) == 1
