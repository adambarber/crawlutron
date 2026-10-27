from __future__ import annotations

import ast
import pathlib
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from functools import cache

from tests.chokepoint_walks import app_trees, local_subtree, nodes_of, resolved_call_name
from tests.invariants._shared import DefinitionNode, _module_definition_index, _relative_app_path
from tests.property_registry import PROPERTY_BY_NAME, PROPERTY_REGISTRY, Property

"""The property registry keeps owners real and detectors honest."""


_DEFINITION_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


_REQUIRED_PROPERTY_FINGERPRINTS = {
    "public-target": frozenset({"ipaddress_range_verdict"}),
    "bool-env": frozenset({"bool_literal_set"}),
    "robots-policy": frozenset(
        {"robots_url", "parse_robots_rules_call", "parse_robots_rules_definition", "robots_bounded_ttl_cache"}
    ),
}


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

    def __init__(self, tree: ast.AST, path: pathlib.Path, prop: Property) -> None:
        self.tree = tree
        self.path = path
        self.prop = prop
        self.counts: dict[str, int] = {}

    def record(self, fingerprint: str, count: int) -> None:
        """Tally `count` more observations of `fingerprint`."""
        self.counts[fingerprint] = self.counts.get(fingerprint, 0) + count


def _property_sites(
    tree: ast.AST, path: pathlib.Path, prop: Property, *, modules: object
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


def _property_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    # Deferred: the observer modules import this module's machinery.
    if prop.name == "public-target":
        from tests.invariants.test_registry_targets import _observe_public_target

        return _observe_public_target(tree, path, prop)
    if prop.name == "bool-env":
        from tests.invariants.test_registry_bool_env import _bool_env_observations

        return _bool_env_observations(tree, path, prop)
    if prop.name == "robots-policy":
        from tests.invariants.test_registry_robots import _observe_robots_policy

        return _observe_robots_policy(tree, path, prop)
    return {}


def _property_registry_failures(
    registry: tuple[Property, ...] = PROPERTY_REGISTRY,
    *,
    trees: tuple[tuple[pathlib.Path, ast.AST], ...] | None = None,
) -> list[str]:
    failures: list[str] = []
    names = [prop.name for prop in registry]
    if len(names) != len(set(names)):
        failures.append("property registry names are not unique")
    observed: dict[str, dict[str, int]] = {prop.name: {} for prop in registry}
    source_trees = app_trees(exclude=frozenset()) if trees is None else trees
    for path, tree in source_trees:
        for prop in registry:
            for fingerprint, count in _property_observations(tree, path, prop).items():
                observed[prop.name][fingerprint] = observed[prop.name].get(fingerprint, 0) + count
    for prop in registry:
        if not prop.fingerprints:
            failures.append(f"{prop.name} has no fingerprints")
        required = _REQUIRED_PROPERTY_FINGERPRINTS.get(prop.name)
        if required is None:
            failures.append(f"{prop.name} has no detector")
        else:
            missing = required - set(prop.fingerprints)
            if missing:
                failures.append(f"{prop.name} is missing required fingerprints: {sorted(missing)}")
        unobserved = set(prop.fingerprints) - set(observed[prop.name])
        if unobserved:
            failures.append(
                f"{prop.name} has unobserved fingerprints (dead configuration): {sorted(unobserved)}"
            )
        zero_observations = {
            fingerprint
            for fingerprint in prop.fingerprints
            if fingerprint in observed[prop.name] and observed[prop.name][fingerprint] <= 0
        }
        if zero_observations:
            failures.append(
                f"{prop.name} has zero observations for fingerprints: {sorted(zero_observations)}"
            )
    return failures


def _observed_property_count(prop: Property) -> int:
    count = 0
    for path, tree in app_trees(exclude=frozenset()):
        count += sum(_property_observations(tree, path, prop).values())
    return count


def test_property_registry_is_not_empty():
    assert PROPERTY_REGISTRY


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


def test_property_registry_owners_exist():
    assert PROPERTY_REGISTRY
    assert all(prop.reason.strip() for prop in PROPERTY_REGISTRY)
    assert all(entry.reason.strip() for prop in PROPERTY_REGISTRY for entry in prop.suppressions)
    for prop in PROPERTY_REGISTRY:
        site_module, site_symbol = prop.definition_site.split(":", 1)
        assert site_module == prop.owner_module
        assert site_symbol in prop.owner_symbols
    definitions: dict[str, list[tuple[str, ast.AST]]] = {}
    for path, tree in app_trees(exclude=frozenset()):
        relative = _relative_app_path(path)
        for node in nodes_of(tree):
            if isinstance(node, _DEFINITION_TYPES):
                definitions.setdefault(node.name, []).append((relative, node))
    failures = _property_owner_failures(PROPERTY_REGISTRY, definitions)
    assert not failures, "property registry owners are not unique: " + ", ".join(failures)


def test_property_registry_fingerprints_are_observed():
    failures = _property_registry_failures()
    assert not failures, "; ".join(failures)


def test_property_registry_rejects_deleted_fingerprint():
    prop = _property("robots-policy")
    weakened = Property(
        name=prop.name,
        owner_module=prop.owner_module,
        owner_symbols=prop.owner_symbols,
        definition_site=prop.definition_site,
        fingerprints=tuple(item for item in prop.fingerprints if item != "robots_url"),
        reason=prop.reason,
        suppressions=prop.suppressions,
    )
    failures = _property_registry_failures((weakened,))
    assert any("robots_url" in failure for failure in failures), failures
