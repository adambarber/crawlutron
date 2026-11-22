"""Readers of the canonical AST must not mutate the object every gate shares."""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import SCAN_ROOTS, canonical_tree, declared_root_files
from tests.chokepoint_paths import REPO_ROOT

_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear", "update", "add", "setattr"})

# The only modules that deliberately mutate a parsed tree: copy-isolation proofs.
# This gate's own literal names the door, so it lists itself.
_COPY_BOUNDARIES = frozenset(
    {
        "tests/test_ast_parse_cache.py",
        "tests/invariants/test_ast_cache_isolation.py",
        "tests/invariants/_clock_family.py",
        "tests/_walk_primitives.py",
        "tests/invariants/test_canonical_tree_read_only.py",
    }
)


def _base_name(node: ast.AST) -> str | None:
    """The root Name of an attribute/subscript chain, for mutation attribution."""
    while isinstance(node, (ast.Attribute, ast.Subscript)):
        node = node.value
    return node.id if isinstance(node, ast.Name) else None


def _canonical_mutation_offenders(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Name every write to a variable bound by canonical_tree in this module."""
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    offenders: list[str] = []
    for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
        if not (isinstance(call.func, ast.Name) and call.func.id == "canonical_tree"):
            continue
        parent = parents.get(call)
        if not isinstance(parent, ast.Assign) or len(parent.targets) != 1:
            continue
        target = parent.targets[0]
        if not isinstance(target, ast.Name):
            continue
        name = target.id
        scope: ast.AST = parent
        while scope in parents and not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            scope = parents[scope]
        for node in ast.walk(scope):
            if node is parent:
                continue
            if isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Delete)):
                targets = list(getattr(node, "targets", []))
                if hasattr(node, "target"):
                    targets.append(node.target)
                if any(_base_name(t) == name for t in targets):
                    offenders.append(f"{path.name}:{node.lineno}: canonical tree {name} mutated")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                receiver = _base_name(node.func.value)
                if node.func.attr in _MUTATORS and (
                    receiver == name or any(_base_name(a) == name for a in node.args)
                ):
                    offenders.append(f"{path.name}:{node.lineno}: canonical tree {name} mutated")
    return offenders


def test_no_reader_mutates_a_canonical_tree() -> None:
    """The canonical tree is shared; a writer would corrupt every gate at once."""
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in declared_root_files(root):
            if path == REPO_ROOT / __file__.replace(".pyc", ".py"):
                continue
            offenders.extend(_canonical_mutation_offenders(canonical_tree(path), path))
    assert not offenders, "canonical trees must be read-only: " + "; ".join(sorted(set(offenders)))


def test_copy_door_is_not_on_the_public_walk_surface() -> None:
    """Reaching for the public door gives a shared tree, never a slow copy."""
    from tests import chokepoint_walks

    assert not hasattr(chokepoint_walks, "parsed")
    assert hasattr(chokepoint_walks, "canonical_tree")


def test_only_copy_boundaries_import_the_private_door() -> None:
    """A new import of _parsed_copy must declare itself a mutation boundary."""
    offenders: list[str] = []
    for root in SCAN_ROOTS:
        for path in declared_root_files(root):
            text = path.read_text(encoding="utf-8")
            if "_parsed_copy" not in text:
                continue
            rel = path.relative_to(REPO_ROOT).as_posix()
            if rel not in _COPY_BOUNDARIES:
                offenders.append(rel)
    assert not offenders, "private copy door imported outside mutation boundaries: " + ", ".join(offenders)


def test_the_detector_names_a_mutation() -> None:
    """A synthetic writer is reported with its file and line, not silently passed."""
    tree = ast.parse("def reader():\n    tree = canonical_tree(p)\n    tree.body.append(x)\n")
    offenders = _canonical_mutation_offenders(tree, pathlib.Path("probe.py"))
    assert offenders == ["probe.py:3: canonical tree tree mutated"]
