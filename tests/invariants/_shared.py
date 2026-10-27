"""The helpers more than one themed invariant module needs.

``assert_trees_clean`` is the one tree-wide sweep every check depends on;
``_enclosing_*`` answer "what def encloses this node?"; function_defs opens scans.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable, Iterable
from pathlib import Path

from tests.chokepoint_walks import REPO_ROOT, app_trees, parsed, relative_posix, resolved_call_name
from tests.property_registry import Property

__all__ = [
    "DefinitionNode",
    "FunctionNode",
    "app_names",
    "assert_trees_clean",
    "function_defs",
    "_module_definition_index",
    "_module_name",
]


def app_names() -> set[str]:
    """Every name the app tree spells, at any depth and in any position.

    Definitions alone are too narrow: attribute spellings exist in app/, so the
    universe is every def/class/attr/alias spelling a detector could target.
    """
    names: set[str] = set()
    for path in (REPO_ROOT / "app").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(parsed(path)):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                names.add(node.name)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.Import | ast.ImportFrom):
                names.update((alias.asname or alias.name).split(".")[0] for alias in node.names)
    return names


type FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def function_defs(tree: ast.AST) -> list[FunctionNode]:
    """Every function definition in ``tree``, sync or async, in source order.

    The opening move of most shape detectors, written once; a parenthesised
    copy in each detector is how two end up walking different node sets.
    """
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def assert_trees_clean(
    detector: Callable[[ast.Module, Path], list[str]],
    message: str,
    *,
    trees: Iterable[tuple[Path, ast.Module]] | None = None,
) -> None:
    """Sweep every module through `detector` and fail on the offenders it finds.

    One tree-wide walk rather than ~60 copied walks; ``trees`` defaults to the
    whole app tree so a forgotten sweep fails loud instead of passing empty.
    """
    offenders = [
        hit for path, tree in (app_trees() if trees is None else trees) for hit in detector(tree, path)
    ]
    assert not offenders, f"{message}: {'; '.join(offenders)}"


def _enclosing_nodes(tree: ast.AST, matches: Callable[[ast.AST], bool]) -> list[tuple[ast.AST, str]]:
    """``(node, innermost enclosing function name)`` for every node ``matches`` accepts.

    ``nodes_of`` has no parent links, so this descends per module to
    read ownership from the enclosing function name via a predicate.
    """
    found: list[tuple[ast.AST, str]] = []

    def visit(node: ast.AST, enclosing: str) -> None:
        for child in ast.iter_child_nodes(node):
            if matches(child):
                found.append((child, enclosing))
            name = (
                child.name
                if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
                else enclosing
            )
            visit(child, name)

    visit(tree, "<module>")
    return found


def _enclosing_calls(tree: ast.AST, target: str) -> list[tuple[ast.Call, str]]:
    """``(call, innermost enclosing function name)`` for calls to ``target``.

    The narrowing ``if`` is what types the result as calls rather than nodes; the
    predicate in :func:`_enclosing_nodes` cannot, because it is a bare callable.
    """
    return [
        (node, enclosing)
        for node, enclosing in _enclosing_nodes(
            tree,
            lambda child: (
                isinstance(child, ast.Call) and resolved_call_name(tree, child).rsplit(".", 1)[-1] == target
            ),
        )
        if isinstance(node, ast.Call)
    ]


# The scope-regex compiler: ONE definition of "compile a user-supplied scope pattern, fail closed, bound the
# length". One definition, one bound, and the fail-closed arms of both.
_SCOPE_COMPILER_NAMES = frozenset({"compile_scope_pattern", "_compile_scope_regex_safe"})


_SCOPE_PATTERN_LENGTH_BOUND = "_MAX_SCOPE_PATTERN_LEN"


def _declared_public_symbols(tree: ast.Module) -> set[str] | None:
    declarations = [
        node
        for node in tree.body
        if (
            isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
        )
        or (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "__all__"
        )
    ]
    if len(declarations) != 1:
        return None
    value = declarations[0].value
    if value is None:
        return None
    try:
        declared = ast.literal_eval(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(declared, (list, tuple, set)) or not all(isinstance(name, str) for name in declared):
        return None
    return set(declared)


# Same shape as the detectors above: an AST walk over app/, a synthetic *_bites companion proving the
# predicate fires, and no hand-maintained exemption list.


def _enclosing_defs(node: ast.AST, root: ast.AST) -> list[str]:
    """The enclosing ``def``/``class`` names from innermost outward.

    AST has no parent pointers, so a parent map is built per tree; the walk is
    O(nodes) and the detectors call it only on a handful of matched nodes.
    """
    parents: dict[int, ast.AST] = {}
    stack = [root]
    while stack:
        parent = stack.pop()
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
            stack.append(child)
    names: list[str] = []
    current: ast.AST | None = node
    while current is not None:
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(current.name)
        current = parents.get(id(current))
    return names


def _relative_app_path(path: pathlib.Path) -> str:
    try:
        return relative_posix(path)
    except ValueError:
        return path.as_posix()


def _synthetic_app_tree(source: str) -> ast.AST:
    """Parse a synthetic module as if it lived at the app-tree root.

    Proof sources are written inline as one string per shape, so a detector
    proof reads as the shape it forbids rather than as harness plumbing.
    """
    return ast.parse(source)


def assert_property_detector_bites(
    prop: Property,
    detector,
    *,
    caught: dict[str, tuple[str | dict[str, str], int]],
    quiet: str | dict[str, str | dict[str, str]],
    quiet_path: pathlib.Path | None = None,
) -> None:
    """Prove a property detector has teeth, in-process, without a break/restore cycle.

    ``caught`` maps a label to (source, exact offender count); a detector that
    cannot recognise its own property fails here, not in production.
    """
    path = REPO_ROOT / "app" / "_synthetic.py"

    def run_case(source: str | dict[str, str], default_path: pathlib.Path) -> list[str]:
        if isinstance(source, str):
            return list(detector(_synthetic_app_tree(source), default_path, prop))
        modules = [
            (REPO_ROOT / relative, ast.parse(module_source)) for relative, module_source in source.items()
        ]
        primary_path, primary_tree = modules[-1]
        return list(detector(primary_tree, primary_path, prop, modules=modules))

    for label, (source, expected) in caught.items():
        offenders = run_case(source, path)
        assert len(offenders) == expected, (
            f"{prop.name} detector: {label} shape produced {len(offenders)} offenders, "
            f"expected {expected}: {offenders}"
        )
    resolved_quiet_path = quiet_path or path
    quiet_sources = quiet if isinstance(quiet, dict) else {"owner delegation": quiet}
    for label, source in quiet_sources.items():
        quiet_offenders = run_case(source, resolved_quiet_path)
        assert not quiet_offenders, f"{prop.name} detector flagged {label}: {quiet_offenders}"


def _module_name(path: pathlib.Path) -> str:
    relative = _relative_app_path(path)
    if relative.endswith("/__init__.py"):
        return relative[: -len("/__init__.py")].replace("/", ".")
    if relative.endswith(".py"):
        return relative[:-3].replace("/", ".")
    return relative.replace("/", ".")


#: A module-level symbol ``_module_definition_index`` records. Naming the three keeps the guarantee where it
#: is established.
type DefinitionNode = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _module_definition_index(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None,
) -> dict[str, list[tuple[pathlib.Path, ast.Module, DefinitionNode]]]:
    index: dict[str, list[tuple[pathlib.Path, ast.Module, DefinitionNode]]] = {}
    for path, tree in modules or ():
        module = _module_name(path)
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
            elif isinstance(node, ast.ClassDef):
                index.setdefault(f"{module}.{node.name}", []).append((path, tree, node))
                for child in node.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.setdefault(f"{module}.{node.name}.{child.name}", []).append((path, tree, child))
    return index
