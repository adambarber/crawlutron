"""The property registry, and the checks the registry exists to support.

A registered property is a claim that a code shape is observed everywhere.
These checks keep the registry non-empty, its owners real, and its detectors
honest, plus the meta-invariants about this test suite itself."""

from __future__ import annotations

import ast
import pathlib
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from functools import cache
from typing import Any

from tests.chokepoint_walks import (
    REPO_ROOT,
    app_trees,
    has_method_call,
    local_call_leaf_names,
    local_nodes,
    local_subtree,
    nodes_of,
    offender,
    resolved_call_name,
)
from tests.invariants._shared import (
    DefinitionNode,
    _module_definition_index,
    _relative_app_path,
    assert_property_detector_bites,
)
from tests.property_registry import PROPERTY_BY_NAME, PROPERTY_REGISTRY, Property

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


def _resolvedlocal_nodes(
    tree: ast.AST,
    node: ast.AST,
    excluded_ids: set[int] | frozenset[int] = frozenset(),
    *,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None = None,
    prop: Property | None = None,
) -> list[tuple[ast.AST, ast.AST]]:
    definitions: dict[str, list[ast.AST]] = {}
    for candidate in nodes_of(tree):
        if isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.setdefault(candidate.name, []).append(candidate)
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
        for child in local_nodes(current):
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


_PUBLIC_RANGE_ATTRIBUTES = frozenset(
    {"is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified"}
)


def _public_range_accesses(nodes: list[tuple[ast.AST, ast.AST]]) -> int:
    count = 0
    for owner_tree, node in nodes:
        for child in local_nodes(node):
            if isinstance(child, ast.Attribute) and child.attr in _PUBLIC_RANGE_ATTRIBUTES:
                count += 1
            elif isinstance(child, ast.Call):
                name = resolved_call_name(owner_tree, child)
                if name.endswith(".ip_network"):
                    count += 1
                elif (
                    name.endswith(tuple(f".{attribute}" for attribute in _PUBLIC_RANGE_ATTRIBUTES))
                    and len(child.args) >= 2
                ):
                    count += 1
    return count


def _public_target_counts(nodes: list[tuple[ast.AST, ast.AST]]) -> tuple[int, int]:
    parses = sum(
        resolved_call_name(owner_tree, call) == "ipaddress.ip_address"
        for owner_tree, node in nodes
        for call in local_nodes(node)
        if isinstance(call, ast.Call)
    )
    return parses, _public_range_accesses(nodes)


class _PropertyObservations:
    """One file's observation walk, and the counts it has tallied so far.

    Every registered property is checked the same way -- walk this file, ask a
    detector what it recognises, return a fingerprint->count map -- so the three
    things that walk needs (the tree, the path, the claim) and the accumulator
    belong together. They were four functions taking the same three positional
    parameters, each declaring its own ``observations: dict[str, int] = {}`` and
    each re-tallying a fingerprint with the same ``get(..., 0) + count``. A
    fourth property needed the same three lines re-spelled, and a count had two
    places to be tallied.
    """

    def __init__(self, tree: ast.AST, path: pathlib.Path, prop: Property) -> None:
        self.tree = tree
        self.path = path
        self.prop = prop
        self.counts: dict[str, int] = {}

    def record(self, fingerprint: str, count: int) -> None:
        """Tally `count` more observations of `fingerprint`."""
        self.counts[fingerprint] = self.counts.get(fingerprint, 0) + count

    def public_target(self) -> dict[str, int]:
        if _relative_app_path(self.path) == self.prop.owner_module:
            owner_ids = _owner_definition_ids(self.tree, self.path, self.prop)
            owner_nodes = [
                node
                for node in nodes_of(self.tree)
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in owner_ids
            ]
            owner_parses, owner_ranges = _public_target_counts([(self.tree, node) for node in owner_nodes])
            if owner_parses and owner_ranges:
                self.record("ipaddress_range_verdict", min(owner_parses, owner_ranges))
            return self.counts
        for _name, _node, nodes in _property_sites(self.tree, self.path, self.prop, modules=None):
            parses, ranges = _public_target_counts(nodes)
            if parses and ranges:
                self.record("ipaddress_range_verdict", min(parses, ranges))
        return self.counts

    def bool_env(self) -> dict[str, int]:
        tree_sets = _bool_env_tree_sets(self.tree, None)
        contexts = _bool_env_contexts(self.tree, self.path, self.prop, None)
        observations = [_bool_env_node_observation(context, tree_sets) for context in contexts]
        if not any(observed for observed, _count in observations):
            return {}
        return {"bool_literal_set": sum(count for _observed, count in observations)}

    def robots_policy(self) -> dict[str, int]:
        contexts = (
            _property_contexts(self.tree)
            if _relative_app_path(self.path) == self.prop.owner_module
            else [node for _name, node in _property_nodes(self.tree, self.path, self.prop)]
        )
        module_paths = _robot_module_paths(self.tree, self.path, None)
        for node in contexts:
            resolved = _resolvedlocal_nodes(self.tree, node, modules=None, prop=self.prop)
            for fingerprint, count in _robot_capabilities_from_nodes(
                self.tree, self.path, self.prop, resolved, module_paths, suppress_shared=False
            ).items():
                self.record(fingerprint, count)
        return self.counts


def _property_sites(
    tree: ast.AST, path: pathlib.Path, prop: Property, *, modules: Any
) -> list[tuple[str, DefinitionNode, list[tuple[ast.AST, ast.AST]]]]:
    """``(name, site node, resolved nodes)`` per site, the owner's own code excluded.

    A registered property is a claim about the whole codebase, so the code that
    *implements* it cannot be evidence for it. Every observation walk therefore
    asks :func:`_owner_definition_ids` what to skip, takes the property's node
    list, and runs the local-resolution pass over what is left. Those three
    steps were spelled out in each detector, and a copy that dropped the
    exclusion would quietly observe the owner believing it had observed the
    codebase -- the one way a registry check can pass without being run.
    """
    excluded = _owner_definition_ids(tree, path, prop)
    return [
        (name, node, _resolvedlocal_nodes(tree, node, excluded, modules=modules, prop=prop))
        for name, node in _property_nodes(tree, path, prop)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


@_module_aware
def _public_target_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    for name, node, nodes in _property_sites(tree, path, prop, modules=_PROPERTY_MODULES.get()):
        parses, ranges = _public_target_counts(nodes)
        if parses and ranges:
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} reimplements public-address policy")
            )
    return offenders


_BOOL_ENV_SPELLINGS = frozenset({"0", "1", "false", "true", "no", "yes", "off", "on"})


# A bool env read is only guarded when the value was normalized first, so these
# are the calls that make one normalized. `getattr(raw, "strip")` counts too:
# it reaches the same method, and recognizing only the attribute form would
# leave a one-token rewrite able to hide the comparison.
_BOOL_ENV_NORMALIZATION_METHODS = frozenset({"strip", "lower", "casefold"})


def _bool_literal_values(node: ast.AST) -> set[str]:
    elements = node.keys if isinstance(node, ast.Dict) else getattr(node, "elts", ())
    return {
        element.value
        for element in elements
        if isinstance(element, ast.Constant) and isinstance(element.value, str)
    }


def _bool_named_sets(nodes) -> dict[str, set[str]]:
    named: dict[str, set[str]] = {}
    for assignment in nodes:
        if not isinstance(assignment, (ast.Assign, ast.AnnAssign)) or not isinstance(
            assignment.value, (ast.Set, ast.List, ast.Tuple, ast.Dict)
        ):
            continue
        literals = _bool_literal_values(assignment.value)
        if not literals & _BOOL_ENV_SPELLINGS:
            continue
        targets = assignment.targets if isinstance(assignment, ast.Assign) else [assignment.target]
        for target in targets:
            if isinstance(target, ast.Name):
                named[target.id] = literals
    return named


def _bool_env_literal_sets(
    nodes: list[tuple[ast.AST, ast.AST]],
) -> tuple[dict[int, set[str]], dict[str, set[str]]]:
    direct: dict[int, set[str]] = {}
    descendants = [child for _owner_tree, node in nodes for child in local_nodes(node)]
    for child in descendants:
        if (
            isinstance(child, (ast.Set, ast.List, ast.Tuple, ast.Dict))
            and _bool_literal_values(child) & _BOOL_ENV_SPELLINGS
        ):
            direct[id(child)] = _bool_literal_values(child)
    return direct, _bool_named_sets(descendants)


def _bool_env_tree_sets(
    tree: ast.AST,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None = None,
) -> dict[str, set[str]]:
    trees = (tree,) if modules is None else tuple(module_tree for _path, module_tree in modules)
    named: dict[str, set[str]] = {}
    for module_tree in trees:
        named.update(_bool_named_sets(nodes_of(module_tree)))
    return named


def _bool_env_node_observation(
    nodes: list[tuple[ast.AST, ast.AST]],
    tree_sets: dict[str, set[str]] | None = None,
) -> tuple[bool, int]:
    direct, named = _bool_env_literal_sets(nodes)
    named = {**tree_sets, **named} if tree_sets else named
    local_nodes_with_tree = [(owner_tree, child) for owner_tree, node in nodes for child in local_nodes(node)]
    has_normalization = any(
        has_method_call(owner_tree, child, _BOOL_ENV_NORMALIZATION_METHODS)
        for owner_tree, child in local_nodes_with_tree
    )
    if not has_normalization:
        return False, 0
    count = 0
    for _owner_tree, child in local_nodes_with_tree:
        if not isinstance(child, ast.Compare) or not any(
            isinstance(op, (ast.In, ast.NotIn)) for op in child.ops
        ):
            continue
        if any(
            id(part) in direct
            or (isinstance(part, ast.Name) and part.id in named)
            or (
                isinstance(part, ast.Attribute)
                and part.attr == "keys"
                and isinstance(part.value, ast.Name)
                and part.value.id in named
            )
            for part in local_nodes(child)
        ):
            count += 1
    return True, count


def _bool_env_contexts(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None,
) -> list[list[tuple[ast.AST, ast.AST]]]:
    excluded = _owner_definition_ids(tree, path, prop)
    if _relative_app_path(path) == prop.owner_module:
        return [
            [(tree, node)]
            for node in nodes_of(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in excluded
        ]
    return [
        _resolvedlocal_nodes(tree, node, excluded, modules=modules, prop=prop)
        for _name, node in _property_nodes(tree, path, prop)
    ]


def _bool_env_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    return _PropertyObservations(tree, path, prop).bool_env()


@_module_aware
def _bool_env_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    tree_sets = _bool_env_tree_sets(tree, _PROPERTY_MODULES.get())
    excluded = _owner_definition_ids(tree, path, prop)
    for name, node in _property_nodes(tree, path, prop):
        context = _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop)
        if _bool_env_node_observation(context, tree_sets)[1]:
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} reimplements boolean environment parsing")
            )
    return offenders


@cache
def _source_and_literals(node: ast.AST) -> tuple[str, str]:
    """Unparse ``node``'s own subtree once, for the signal predicates below.

    Cached on the node, and the cache is the point. ``_robot_signal`` asks this
    question of every node in every module, and ``_parser_definition`` asks it
    again about the same nodes; without the cache each node's whole subtree is
    re-serialised by each caller. The cost is superlinear in tree depth rather
    than in file count, because a nested node's subtree is unparsed again by
    every ancestor that asks -- which is what made the robots-policy sweep the
    most expensive gate in the suite rather than one of dozens of equal ones.

    Keying on the node is safe without an explicit identity: ``ast.AST`` defines
    no ``__eq__``, so hashing is by object identity, and the trees these nodes
    belong to are already held alive for the process by the parsed-tree cache.
    """
    nodes = local_subtree(node)
    source = (
        " ".join(ast.unparse(child) for child in nodes[1:]).lower()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        else ast.unparse(node).lower()
    )
    literals = "".join(
        child.value for child in nodes if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ).lower()
    return source, literals


def _robot_signal(node: ast.AST) -> bool:
    source, literals = _source_and_literals(node)
    return any(
        token in source or token in literals for token in ("robot", "crawl-delay", "disallow", "user-agent")
    )


def _call_leaf_names(tree: ast.AST, node: ast.AST):
    for child in local_nodes(node):
        if isinstance(child, ast.Call):
            yield resolved_call_name(tree, child).rsplit(".", 1)[-1].lower()


# `"{}/robots.txt".format(base)` and `"/robots.txt".join(...)` build a URL the
# same way a BinOp or f-string does, so the URL literal detector must see them.
# `getattr(x, "format")` reaches the same method through a token.
_STRING_CONSTRUCTOR_METHODS = frozenset({"format", "join"})


_URL_CONSTRUCTOR_METHODS = frozenset({"urljoin", "urlunsplit"})


# How a line-oriented robots file gets torn into lines. A hand-rolled scanner
# built on anything else is the duplicated robots parser this detector bans.
_ROBOTS_LINE_SPLIT_METHODS = frozenset({"splitlines", "partition"})


def _fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        any(token in name for token in ("fetch", "request", "download", "retrieve", "http"))
        for name in _call_leaf_names(tree, node)
    )


def _string_constructor(tree: ast.AST, node: ast.AST) -> bool:
    return has_method_call(tree, node, _STRING_CONSTRUCTOR_METHODS)


def _url_literal_capability(tree: ast.AST, node: ast.AST) -> bool:
    if not isinstance(node, (ast.BinOp, ast.JoinedStr, ast.Call)):
        return False
    nodes = local_subtree(node)
    if not any(isinstance(child, (ast.BinOp, ast.JoinedStr)) for child in nodes) and not _string_constructor(
        tree, node
    ):
        return False
    parts = [
        child.value for child in nodes if isinstance(child, ast.Constant) and isinstance(child.value, str)
    ]
    return any("robot" in part or ".txt" in part for part in parts) and any(
        part.lstrip("{}").startswith("/") or "http" in part for part in parts
    )


def _url_constructor(tree: ast.AST, node: ast.AST) -> bool:
    nodes = local_subtree(node)
    return has_method_call(tree, node, _URL_CONSTRUCTOR_METHODS) or any(
        _url_literal_capability(tree, child) for child in nodes
    )


def _robots_fetch_like(tree: ast.AST, node: ast.AST) -> bool:
    return any(
        "http" in name
        or name.startswith(("load_", "retrieve", "request"))
        or name == "fetch_and_decode"
        or (name in {"get", "open", "read", "send", "urlopen"} and _robot_signal(node))
        for name in _call_leaf_names(tree, node)
    )


def _parser_call_count(tree: ast.AST, node: ast.AST) -> int:
    return sum(
        1
        for name, _call in local_call_leaf_names(tree, node)
        if ("parse" in name and any(token in name for token in ("robot", "rule", "directive", "crawl")))
        or ("decode" in name and "rule" in name)
        or ("read" in name and "robot" in name)
    )


def _parser_definition(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    _source, _literals = _source_and_literals(node)
    name = node.name.lower()
    if _relative_app_path(path) == "app/crawl/robots_parse.py" and ("robot" in name or "parse" in name):
        return True
    if "robot" in name and any(token in name for token in ("parse", "rule", "directive")):
        return True
    nodes = local_subtree(node)
    line_scan = has_method_call(tree, node, _ROBOTS_LINE_SPLIT_METHODS)
    collection_return = any(
        isinstance(child, ast.Return)
        and isinstance(child.value, (ast.ListComp, ast.List, ast.Tuple, ast.Dict, ast.DictComp))
        for child in nodes
    )
    return line_scan and collection_return


def _cache_call_count(tree: ast.AST, node: ast.AST) -> int:
    return sum(
        isinstance(child, ast.Call)
        and resolved_call_name(tree, child).rsplit(".", 1)[-1].lower().endswith("boundedttlcache")
        for child in local_nodes(node)
    )


def _robots_observations_for_node(tree: ast.AST, path: pathlib.Path, node: ast.AST) -> dict[str, int]:
    observations: dict[str, int] = {}
    parser_calls = _parser_call_count(tree, node)
    parser_definitions = int(_parser_definition(tree, path, node))
    cache_calls = _cache_call_count(tree, node)
    url_calls = int(_url_constructor(tree, node))
    if url_calls and _robot_signal(node) and _robots_fetch_like(tree, node):
        observations["robots_url"] = url_calls
    if parser_calls:
        observations["parse_robots_rules_call"] = parser_calls
    if parser_definitions:
        observations["parse_robots_rules_definition"] = parser_definitions
    if cache_calls and (_robot_signal(node) or parser_calls or _fetch_like(tree, node)):
        observations["robots_bounded_ttl_cache"] = cache_calls
    return observations


def _robot_module_paths(
    tree: ast.AST,
    path: pathlib.Path,
    modules: tuple[tuple[pathlib.Path, ast.Module], ...] | None,
) -> dict[int, pathlib.Path]:
    module_paths = {id(module_tree): module_path for module_path, module_tree in modules or ()}
    module_paths.setdefault(id(tree), path)
    return module_paths


def _robot_capabilities_from_nodes(
    tree: ast.AST,
    path: pathlib.Path,
    prop: Property,
    nodes: list[tuple[ast.AST, ast.AST]],
    module_paths: dict[int, pathlib.Path],
    *,
    suppress_shared: bool,
) -> dict[str, int]:
    capabilities: dict[str, int] = {}
    for owner_tree, resolved in nodes:
        owner_path = module_paths.get(id(owner_tree), path)
        for capability, count in _robots_observations_for_node(owner_tree, owner_path, resolved).items():
            if (
                suppress_shared
                and resolved is not nodes[0][1]
                and _robots_shared_parser(prop, owner_path, resolved, owner_tree, capability)
            ):
                continue
            capabilities[capability] = capabilities.get(capability, 0) + count
    return capabilities


def _robots_policy_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    return _PropertyObservations(tree, path, prop).robots_policy()


def _robots_shared_parser(
    prop: Property,
    path: pathlib.Path,
    node: ast.AST,
    tree: ast.AST,
    capability: str,
) -> bool:
    return (
        capability == "parse_robots_rules_definition"
        and _relative_app_path(path) != prop.owner_module
        and _parser_definition(tree, path, node)
        and not _robots_fetch_like(tree, node)
        and not _url_constructor(tree, node)
        and not _cache_call_count(tree, node)
    )


@_module_aware
def _robots_policy_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders: list[str] = []
    excluded = _owner_definition_ids(tree, path, prop)
    module_paths = _robot_module_paths(tree, path, _PROPERTY_MODULES.get())
    for name, node in _property_nodes(tree, path, prop):
        nodes = _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop)
        capabilities = _robot_capabilities_from_nodes(
            tree, path, prop, nodes, module_paths, suppress_shared=True
        )
        if "robots_url" not in capabilities and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            builders = {
                resolved.name
                for _owner_tree, resolved in nodes
                if isinstance(resolved, (ast.FunctionDef, ast.AsyncFunctionDef))
                and _url_constructor(_owner_tree, resolved)
                and _robot_signal(resolved)
                and not _robots_fetch_like(_owner_tree, resolved)
            }
            local_calls = {
                child.func.id
                if isinstance(child.func, ast.Name)
                else resolved_call_name(tree, child).rsplit(".", 1)[-1]
                for child in local_nodes(node)
                if isinstance(child, ast.Call)
            }
            if builders & local_calls and _robots_fetch_like(tree, node):
                capabilities["robots_url"] = 1
        for capability in sorted(capabilities):
            if _property_suppressed(prop, path, name, capability):
                continue
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} has robots capability {capability}")
            )
    return offenders


def _property_observations(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    walk = _PropertyObservations(tree, path, prop)
    if prop.name == "public-target":
        return walk.public_target()
    if prop.name == "bool-env":
        return walk.bool_env()
    if prop.name == "robots-policy":
        return walk.robots_policy()
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


def test_property_registry_detector_bites():
    prop = _property("bool-env")
    path = REPO_ROOT / "app" / "_zero_observation.py"
    tree = ast.parse("def normalize(value):\n    return value.strip().lower()\n")
    assert _bool_env_observations(tree, path, prop) == {"bool_literal_set": 0}
    failures = _property_registry_failures((prop,), trees=((path, tree),))
    assert any("zero observations" in failure and "bool_literal_set" in failure for failure in failures), (
        failures
    )


#: A module that defines ``parse_bool_value`` twice -- the shape both registry
#: proofs below are about. Named because each carried its own copy of the source
#: and its own re-walk for the duplicate definitions, so a change to the synthetic
#: definition had to be made twice to be made at all, and a change to the walk had
#: to be made once and forgotten once.
DUPLICATE_DEF_SOURCE = (
    "def parse_bool_value(raw, default, *, env_name):\n"
    "    return default\n"
    "def parse_bool_value(raw, default, *, env_name):\n"
    "    return raw.strip().lower() in {'0', 'false', 'no', 'off'}\n"
)


def _duplicate_def_nodes(tree: ast.AST) -> list[ast.FunctionDef]:
    """Every ``parse_bool_value`` definition in ``tree``, in source order."""
    return [
        node
        for node in nodes_of(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "parse_bool_value"
    ]


def test_property_registry_rejects_duplicate_owner_count():
    prop = _property("bool-env")
    tree = ast.parse(DUPLICATE_DEF_SOURCE)
    definitions = {"parse_bool_value": [("app/env.py", node) for node in _duplicate_def_nodes(tree)]}
    assert _property_owner_failures((prop,), definitions) == ["parse_bool_value=2"]


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


def test_property_owner_duplicate_is_not_sanctioned():
    prop = _property("bool-env")
    tree = ast.parse(DUPLICATE_DEF_SOURCE)
    path = REPO_ROOT / "app" / "env.py"
    owner_ids = _owner_definition_ids(tree, path, prop)
    duplicate = _duplicate_def_nodes(tree)[1]
    assert len(owner_ids) == 1
    assert not _property_owner(path, prop, tree=tree, node=duplicate)
    assert _bool_env_offenders(tree, path, prop)


def test_public_target_has_one_implementation():
    prop = _property("public-target")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_public_target_offenders(tree, path, prop))
    assert _observed_property_count(prop), "public-target detector observed no fingerprint occurrences"
    assert not offenders, "public-address policy has a second implementation: " + ", ".join(offenders)


def test_public_target_registry_is_not_empty():
    assert _property("public-target").fingerprints


def test_public_target_detector_bites():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            # All four alias forms live in ONE module on purpose: a detector
            # that only inspected a module's first function would pass if each
            # shape were proven in its own module.
            "all alias forms in one module": (
                "import ipaddress\n"
                "import ipaddress as ip\n"
                "from ipaddress import ip_address as parse\n"
                "IP = ipaddress\n"
                "def alias_import(value):\n"
                "    address = ip.ip_address(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def from_import(value):\n"
                "    address = parse(value)\n"
                "    return address.is_private or ip.ip_network('100.64.0.0/10')\n"
                "def assignment(value):\n"
                "    address = IP.ip_address(value)\n"
                "    return address.is_private or IP.ip_network('100.64.0.0/10')\n"
                "def getattr_call(value):\n"
                "    address = getattr(ipaddress, 'ip_address')(value)\n"
                "    return address.is_private or ipaddress.ip_network('100.64.0.0/10')\n",
                4,
            ),
            "helper split with dynamic range access": (
                "import ipaddress\n"
                "def parse_address(value):\n"
                "    return ipaddress.ip_address(value)\n"
                "def verdict(value):\n"
                "    return not getattr(parse_address(value), 'is_private')\n",
                1,
            ),
            "aliased getattr": (
                "from builtins import getattr as ga\n"
                "import ipaddress\n"
                "def verdict(value):\n"
                "    return not ga(ipaddress, 'ip_address')(value).is_private\n",
                1,
            ),
            "constant-folded getattr name": (
                "import ipaddress\n"
                "def verdict(value):\n"
                "    name = 'ip_' + 'address'\n"
                "    return not getattr(ipaddress, name)(value).is_private\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import _is_public_ip as verdict\n"
            "def check(value):\n"
            "    return verdict(value)\n"
        ),
    )
    runtime_name = ast.parse(
        "import ipaddress\n"
        "def attribute_name():\n"
        "    return 'ip_address'\n"
        "def verdict(value):\n"
        "    name = attribute_name()\n"
        "    return not getattr(ipaddress, name)(value).is_private\n"
    )
    assert not _public_target_offenders(
        runtime_name,
        REPO_ROOT / "app" / "_synthetic.py",
        _property("public-target"),
    )


def test_bool_env_has_one_implementation():
    prop = _property("bool-env")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_bool_env_offenders(tree, path, prop))
    assert _observed_property_count(prop), "bool-env detector observed no fingerprint occurrences"
    assert not offenders, "boolean environment policy has a second implementation: " + ", ".join(offenders)


def test_bool_env_registry_is_not_empty():
    assert _property("bool-env").fingerprints


def test_bool_env_detector_bites():
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "truthy set": (
                "def parse(value):\n    return value.strip().lower() in {'1', 'true', 'yes', 'on'}\n",
                1,
            ),
            "inverted set": (
                "def parse(value):\n    return value.strip().lower() not in {'0', 'false', 'no', 'off'}\n",
                1,
            ),
            "module-level set": (
                "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                "def parse(value):\n"
                "    return value.strip().lower() not in FALSE_WORDS\n",
                1,
            ),
            "helper split through dict keys": (
                "FALSE_WORDS = {'0': False, 'false': False, 'no': False, 'off': False}\n"
                "def normalize(value):\n"
                "    return value.strip().lower()\n"
                "def parse(value):\n"
                "    return normalize(value) in FALSE_WORDS.keys()\n",
                1,
            ),
        },
        quiet=(
            "from app.validation import parse_bool_value as parse\n"
            "def parse_env(value, default):\n"
            "    return parse(value, default, env_name='X')\n"
        ),
    )


def test_robots_policy_has_one_implementation():
    prop = _property("robots-policy")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(_robots_policy_offenders(tree, path, prop))
    assert _observed_property_count(prop), "robots-policy detector observed no fingerprint occurrences"
    assert not offenders, "robots policy has a second implementation: " + ", ".join(offenders)


def test_robots_policy_registry_is_not_empty():
    assert _property("robots-policy").fingerprints


def test_robots_policy_detector_bites():
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "parser call and cache": (
                "from app.bounded_ttl import BoundedTTLCache as Cache\n"
                "from app.crawl.robots_parse import parse_robots_rules as parse\n"
                "def robots_loader(origin):\n"
                "    cache = Cache(max_entries=1, ttl=1)\n"
                "    return parse('User-agent: *')\n",
                2,
            ),
            "parser definition": (
                "def parse_robots_rules(text):\n    return [(True, text)]\n",
                1,
            ),
            "split url literal": (
                "def renamed_loader(origin):\n"
                "    path = '/r' + 'obots.txt'\n"
                "    return http_fetcher(urljoin(origin, path))\n",
                1,
            ),
            "renamed parser definition": (
                "def decode_directives(text):\n"
                "    rows = text.splitlines()\n"
                "    return [(True, row) for row in rows if 'disallow' in row.lower()]\n",
                1,
            ),
            "helper split with formatted url": (
                "def endpoint(origin):\n"
                "    return '{}/robots.txt'.format(origin)\n"
                "def load(origin):\n"
                "    return http_fetcher(endpoint(origin))\n",
                1,
            ),
            "inline joined url": (
                "def load(origin):\n    return http_fetcher(''.join((origin, '/robots.txt')))\n",
                1,
            ),
            "inline f-string url": (
                "def load(origin):\n    return http_fetcher(f'{origin}/robots.txt')\n",
                1,
            ),
            "inline absolute formatted url": (
                "def load(origin):\n    return http_fetcher('https://{}/robots.txt'.format(origin))\n",
                1,
            ),
            "helper split with arbitrary dict parser": (
                "def directives(text):\n"
                "    return {line: True for line in text.splitlines()}\n"
                "def load(text):\n"
                "    return http_fetcher(directives(text))\n",
                1,
            ),
        },
        quiet=(
            "from app.crawl.robots import load_robots_rules as load\n"
            "def check(origin):\n"
            "    return load(origin, fetcher, 5)\n"
        ),
    )


def test_property_detectors_resolve_imported_helpers_across_modules():
    assert_property_detector_bites(
        _property("public-target"),
        _public_target_offenders,
        caught={
            "imported verdict helper": (
                {
                    "app/_public_helper.py": (
                        "import ipaddress\n"
                        "def parse_address(value):\n"
                        "    return ipaddress.ip_address(value)\n"
                    ),
                    "app/_public_consumer.py": (
                        "from app._public_helper import parse_address\n"
                        "def verdict(value):\n"
                        "    address = parse_address(value)\n"
                        "    return address.is_private\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_public_owner.py": "def verdict(value):\n    return value\n",
                "app/_public_consumer.py": (
                    "from app._public_owner import verdict\ndef check(value):\n    return verdict(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("bool-env"),
        _bool_env_offenders,
        caught={
            "imported normalization helper": (
                {
                    "app/_bool_helper.py": ("def normalize(value):\n    return value.strip().lower()\n"),
                    "app/_bool_consumer.py": (
                        "from app._bool_helper import normalize\n"
                        "FALSE_WORDS = {'0', 'false', 'no', 'off'}\n"
                        "def parse(value):\n"
                        "    return normalize(value) in FALSE_WORDS\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "imported owner": {
                "app/_bool_owner.py": "def parse(value):\n    return value\n",
                "app/_bool_consumer.py": (
                    "from app._bool_owner import parse\ndef check(value):\n    return parse(value)\n"
                ),
            },
        },
    )
    assert_property_detector_bites(
        _property("robots-policy"),
        _robots_policy_offenders,
        caught={
            "imported policy helper": (
                {
                    "app/_robots_helper.py": (
                        "def parse_robot_policy(text):\n"
                        "    return [(True, text)]\n"
                        "def load_policy(text):\n"
                        "    return parse_robot_policy(text)\n"
                    ),
                    "app/_robots_consumer.py": (
                        "from app._robots_helper import load_policy\n"
                        "def load(text):\n"
                        "    return load_policy(text)\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "shared field parser": {
                "app/_robots_primitive.py": (
                    "def parse_fields(text):\n    return [(True, line) for line in text.splitlines()]\n"
                ),
                "app/_robots_consumer.py": (
                    "from app._robots_primitive import parse_fields\n"
                    "def load(text):\n"
                    "    return parse_fields(text)\n"
                ),
            },
        },
    )
