"""Every walk-based gate's scan scope is the derived surface, never a local tuple."""

from __future__ import annotations

import ast
import pathlib

import yaml

from scripts.gate_fanout import SCAN_ROOTS_MARKER
from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS, declared_root_files

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
    probe = ast.parse(
        "x = ('packages/core', 'packages/mcp', 'packages/rest', 'packages/sdk', 'scripts', 'tests')\n"
    )
    assert _local_root_tuples(probe), "audit lost the full-surface shape"
    subset = ast.parse("x = ('scripts', 'tests')\n")
    assert _local_root_tuples(subset), "audit lost the subset shape"


#: The one sanctioned home of an app-root path literal: the APP_ROOT derivation.
_APP_ROOT_OWNER = "tests/_walk_primitives.py"


def _app_root_literal_hits(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Every ``REPO_ROOT / "crawlutron"...`` expression: a walk fed by a hand-listed
    app path that a layout move would silently vacate (the scanner greens on nothing)."""
    hits: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.BinOp) or not isinstance(node.op, ast.Div):
            continue
        left = node.left
        if not (
            isinstance(left, ast.Name)
            and left.id == "REPO_ROOT"
            and isinstance(node.right, ast.Constant)
            and isinstance(node.right.value, str)
            and node.right.value.split("/", 1)[0] == "crawlutron"
        ):
            continue
        hits.append(str(node.lineno))
    return hits


def test_no_app_root_path_literal_survives_the_derived_door() -> None:
    """Zero ``REPO_ROOT / "crawlutron"`` hand-lists in tests/: every walk feeds the
    derived APP_ROOT door, so a layout move under Wave A cannot silently empty a scan."""
    offenders: list[str] = []
    for path in declared_root_files("tests"):
        if path.relative_to(REPO_ROOT).as_posix() == _APP_ROOT_OWNER:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno in _app_root_literal_hits(tree, path):
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, "REPO_ROOT / 'crawlutron' hand-lists must use the derived APP_ROOT:\n" + "\n".join(
        offenders
    )


def test_the_root_literal_audit_detects_a_planted_literal() -> None:
    """The detector fires on any app-root path literal, including the sanctioned owner's."""

    violation = ast.parse("x = REPO_ROOT / 'crawlutron' / 'crawl'\n")
    assert _app_root_literal_hits(violation, pathlib.Path("x.py")) == ["1"]
    owner_shape = ast.parse("APP_ROOT = REPO_ROOT / 'crawlutron'\n")
    assert _app_root_literal_hits(owner_shape, pathlib.Path("x.py")) == ["1"]


#: The pre-commit gate whose scan command must name the derived surface. This
#: list is the audit's own declaration; a gate added here must use the token.
_PRECOMMIT_CONFIG = REPO_ROOT / ".pre-commit-config.yaml"


def _scan_literal_offenders(config: pathlib.Path) -> list[str]:
    """Every hook whose command or files-trigger hard-codes a scan-root path."""
    document = yaml.safe_load(config.read_text(encoding="utf-8"))
    offenders: list[str] = []
    for repo in document["repos"]:
        for hook in repo.get("hooks", ()):
            hook_id = hook["id"]
            if hook_id in {"commit-citation"}:
                continue
            entry = hook.get("entry", "")
            command = entry.split(" -- ", 1)[1] if " -- " in entry else ""
            roots_ = {arg for arg in command.split() if arg in SCAN_ROOTS}
            for root in roots_:
                offenders.append(f"{hook_id}: hard-coded scan root {root!r} (use {SCAN_ROOTS_MARKER})")
            file_trigger = hook.get("files", "")
            for root in SCAN_ROOTS:
                if root in file_trigger:
                    offenders.append(f"{hook_id}: files-trigger hard-codes root {root!r}")
    return offenders


def test_every_precommit_run_gate_names_the_derived_scan_surface() -> None:
    """A hook entry that hard-codes a scan-root path literal is the same drift the AST
    audit kills: ruff/vulture carrying a root tuple quietly skip a tree that lands
    later. The declaration must name the token; gate_fanout expands it to the surface."""
    offenders = _scan_literal_offenders(_PRECOMMIT_CONFIG)
    assert not offenders, "\n".join(offenders)


def test_the_scan_surface_audit_bites_a_hand_listed_scan() -> None:
    """The audit must red on a hook that hard-codes one root, so a future
    regression to a literal is a finding, not a silent narrowing."""
    # Clone the real config text with the token replaced by one root literal.
    live = _PRECOMMIT_CONFIG.read_text(encoding="utf-8")
    mutant_text = live.replace("ruff check {{SCAN_ROOTS}}", "ruff check packages/core")
    mutant = REPO_ROOT / "tests" / "invariants" / "._scan_mutant.yaml"
    mutant.write_text(mutant_text, encoding="utf-8")
    try:
        hits = _scan_literal_offenders(mutant)
    finally:
        mutant.unlink(missing_ok=True)
    assert hits and any("packages/core" in h for h in hits), "audit lost the literal scan shape"
