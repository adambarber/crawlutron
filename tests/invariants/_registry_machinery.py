"""The property-registry machinery the registry detector modules share.

Moved here (#476) from test_registry.py: four detector modules were importing
this machinery from a sibling test module's internals; shared machinery lives
on the declared support surface, not in another test's namespace.
"""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from functools import cache

from tests.chokepoint_walks import local_subtree, nodes_of, resolved_call_name
from tests.invariants._shared import DefinitionNode, _module_definition_index, _relative_app_path
from tests.property_registry import PROPERTY_BY_NAME, Property

_DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)

__all__ = [
    "PROPERTY_BY_NAME",
    "_PROPERTY_MODULES",
    "Property",
    "_PropertyObservations",
    "_module_aware",
    "_observed_property_count",
    "_owner_definition_ids",
    "_property",
    "_property_contexts",
    "_property_nodes",
    "_property_owner",
    "_property_owner_failures",
    "_property_sites",
    "_property_suppressed",
    "_resolvedlocal_nodes",
]


def _property(name: str) -> Property:
    return PROPERTY_BY_NAME[name]


def _property_contexts(tree: ast.AST) -> list[ast.AST]:
    symbol_types = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
    contexts: list[ast.AST] = [
        node for node in nodes_of(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    for scope in (tree, *(node for node in nodes_of(tree) if isinstance(node, ast.ClassDef))):
        contexts.extend(node for node in getattr(scope, "body", []) if not isinstance(node, symbol_types))
    return contexts


_PROPERTY_MODULES: ContextVar[tuple[tuple[pathlib.Path, ast.Module], ...] | None] = ContextVar(
    "property_modules", default=None
)


def _module_aware(detector):
    def wrapped(tree, path, prop, *, modules=None):
        token = _PROPERTY_MODULES.set(modules)
        try:
            return detector(tree, path, prop)
        finally:
            _PROPERTY_MODULES.reset(token)

    return wrapped


@cache
def _tree_definitions(tree: ast.AST) -> dict[str, list[ast.AST]]:
    """Every function name in `tree` -> its definitions, built once per tree.

    Keyed by identity like ``nodes_of``: the resolution loop asks it for every
    site, and a per-call rebuild was the registry gates' dominant cost.
    """
    definitions: dict[str, list[ast.AST]] = {}
    for candidate in nodes_of(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(candidate.name, []).append(candidate)
    return definitions


def _resolvedlocal_nodes(
    tree: ast.AST,
    node: ast.AST,
    excluded_ids: set[int] | frozenset[int] = frozenset(),
    *,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None = None,
    prop: Property | None = None,
) -> list[tuple[ast.AST, ast.AST]]:
    definitions = _tree_definitions(tree)
    module_index = _module_definition_index(modules)
    resolved: list[tuple[ast.AST, ast.AST]] = []
    seen: set[tuple[int, int]] = set()
    pending: list[tuple[ast.AST, ast.AST]] = [(tree, node)]
    while pending:
        current_tree, current = pending.pop()
        current_key = (id(current_tree), id(current))
        if current_key in seen or id(current) in excluded_ids:
            continue
        seen.add(current_key)
        resolved.append((current_tree, current))
        for child in local_subtree(current):
            if not isinstance(child, ast.Call):
                continue
            if isinstance(child.func, ast.Name):
                matches = definitions.get(child.func.id, [])
                if len(matches) == 1 and id(matches[0]) not in excluded_ids:
                    pending.append((current_tree, matches[0]))
            if not module_index:
                continue
            module_matches = module_index.get(resolved_call_name(current_tree, child), [])
            if len(module_matches) != 1:
                continue
            candidate_path, candidate_tree, candidate = module_matches[0]
            if (
                prop is not None
                and _relative_app_path(candidate_path) == prop.owner_module
                and candidate.name in prop.owner_symbols
            ):
                continue
            pending.append((candidate_tree, candidate))
    return resolved


def _owner_definition_ids(tree: ast.AST, path: pathlib.Path, prop: Property) -> set[int]:
    if _relative_app_path(path) != prop.owner_module:
        return set()
    by_symbol: dict[str, list[ast.AST]] = {}
    for node in nodes_of(tree):
        if isinstance(node, _DEFINITION_TYPES) and node.name in prop.owner_symbols:
            by_symbol.setdefault(node.name, []).append(node)
    owner_ids: set[int] = set()
    for nodes in by_symbol.values():
        owner_ids.add(id(nodes[0]))
    return owner_ids


def _property_owner(
    path: pathlib.Path,
    prop: Property,
    symbol: str | None = None,
    *,
    tree: ast.AST | None = None,
    node: ast.AST | None = None,
) -> bool:
    if node is not None and tree is not None:
        return id(node) in _owner_definition_ids(tree, path, prop)
    return False


def _property_suppressed(prop: Property, path: pathlib.Path, symbol: str, fingerprint: str) -> bool:
    relative = _relative_app_path(path)
    return any(
        entry.module == relative and entry.symbol == symbol and entry.fingerprint == fingerprint
        for entry in prop.suppressions
    )


def _property_nodes(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[tuple[str, ast.AST]]:
    owner_ids = _owner_definition_ids(tree, path, prop)
    return [
        (getattr(node, "name", "<module>"), node)
        for node in _property_contexts(tree)
        if id(node) not in owner_ids
    ]


class _PropertyObservations:
    """One file's observation walk and the counts it has tallied: the tree, the path
    and the claim travel with the accumulator because every property is checked the
    same way, where four functions once re-spelled the trio and the tally.
    """

    def __init__(self, tree: ast.AST, path: ast.AST | pathlib.Path, prop: Property) -> None:
        self.tree = tree
        self.path = path
        self.prop = prop
        self.counts: dict[str, int] = {}

    def record(self, fingerprint: str, count: int) -> None:
        """Tally `count` more observations of `fingerprint`."""
        self.counts[fingerprint] = self.counts.get(fingerprint, 0) + count


def _property_sites(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    *,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None = None,
) -> list[tuple[str, DefinitionNode, list[tuple[ast.AST, ast.AST]]]]:
    """``(name, site node, resolved nodes)`` per site, the owner's own code excluded: a
    registered property is a claim about the whole codebase, so the code that implements
    it cannot be its own evidence -- the owner exclusion is the one step a copy could drop.
    """
    excluded = _owner_definition_ids(tree, path, prop)
    return [
        (name, node, _resolvedlocal_nodes(tree, node, excluded, modules=modules, prop=prop))
        for name, node in _property_nodes(tree, path, prop)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


#: Per-property observation walks, registered by the detector modules at import
#: time; registering (instead of importing them here) keeps the graph one-directional.
_OBSERVERS: dict[str, Callable[..., dict[str, int]]] = {}


def _property_observations(tree: ast.AST, path: ast.AST | pathlib.Path, prop: Property) -> dict[str, int]:
    observer = _OBSERVERS.get(prop.name)
    if observer is None:
        return {}
    return observer(tree, path, prop)


def _observed_property_count(prop: Property) -> int:
    from tests.chokepoint_walks import app_trees

    count = 0
    for path, tree in app_trees(exclude=frozenset()):
        count += sum(_property_observations(tree, path, prop).values())
    return count


def _property_owner_failures(
    registry: tuple[Property, ...],
    definitions: Mapping[str, Sequence[tuple[str, ast.AST]]],
) -> list[str]:
    failures: list[str] = []
    for prop in registry:
        for symbol in prop.owner_symbols:
            matches = definitions.get(symbol, [])
            if len(matches) != 1:
                failures.append(f"{symbol}={len(matches)}")
                continue
            relative, _node = matches[0]
            if relative != prop.owner_module:
                failures.append(f"{symbol}@{relative}")
    return failures
