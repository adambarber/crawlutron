"""Every walk-based gate's scan scope is the derived surface, never a local tuple."""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS

#: The root-literal tuples the completeness gate names; SCAN_ROOTS members
#: appearing as a local loop/parametrize tuple are the drift machine.
_ROOT_NAMES = frozenset(SCAN_ROOTS)


def _local_root_tuples(tree: ast.Module) -> list[tuple[int, ast.Tuple]]:
    """Every tuple literal whose items are ALL scan-root name strings."""
    hits: list[tuple[int, ast.Tuple]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Tuple):
            continue
        if not node.elts:
            continue
        values = [elt.value for elt in node.elts if isinstance(elt, ast.Constant)]
        if (
            len(values) == len(node.elts)
            and len(values) >= 2
            and all(isinstance(v, str) and v in _ROOT_NAMES for v in values)
            and len(set(values)) == len(values)
        ):
            hits.append((node.lineno, node))
    return hits


def _gate_files() -> list[pathlib.Path]:
    """The gate population the invariant audits: the invariant suite itself,
    minus the scope law's own declaration file (its equality pin on the exact
    tuple is the law stated, not a gate scope)."""
    return sorted(
        p
        for p in (REPO_ROOT / "tests" / "invariants").glob("*.py")
        if p.name not in {"__pycache__", "test_scan_roots.py"}
    )


def _tuple_names(node: ast.Tuple) -> list[str]:
    """The string constants of a root tuple's elements, typed for the checker."""
    names: list[str] = []
    for elt in node.elts:
        if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
            names.append(elt.value)
    return names


def test_no_invariant_gate_carries_a_private_root_tuple() -> None:
    """A local tuple of root names in a gate file is a hand list behind a green: a gate loops
    ("crawlutron", "tests") while the repo holds a third tree and the new tree is scanned by
    nothing -- the defect class that landed twice (the app/ rename, crawlutron_sdk's arrival)."""
    offenders: list[str] = []
    for path in _gate_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, node in _local_root_tuples(tree):
            where = f"{path.relative_to(REPO_ROOT)}:{lineno}"
            offenders.append(f"{where}: local root tuple {tuple(_tuple_names(node))}")
    assert not offenders, "\n".join(offenders)


def test_the_completeness_audit_itself_spans_the_derived_surface() -> None:
    """The audit is not a smaller hand list wearing a badge: it must accept a
    synthetic gate whose tuple matches SCAN_ROOTS exactly (drift-proof both ways)."""
    probe = ast.parse("x = ('crawlutron', 'crawlutron_sdk', 'scripts', 'tests')\n")
    assert _local_root_tuples(probe), "audit lost the full-surface shape"
    subset = ast.parse("x = ('crawlutron', 'tests')\n")
    assert _local_root_tuples(subset), "audit lost the subset shape"
