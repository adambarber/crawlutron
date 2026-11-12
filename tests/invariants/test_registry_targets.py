"""The public-target registry: one implementation, observed everywhere."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import (
    APP_ROOT,
    app_trees,
    local_subtree,
    nodes_of,
    offender,
    resolved_call_name,
)
from tests.invariants._registry_machinery import (
    _OBSERVERS,
    _PROPERTY_MODULES,
    _module_aware,
    _observed_property_count,
    _owner_definition_ids,
    _property,
    _property_sites,
    _PropertyObservations,
    _relative_app_path,
)
from tests.property_registry import Property

_PUBLIC_RANGE_ATTRIBUTES = frozenset(
    {"is_private", "is_loopback", "is_link_local", "is_reserved", "is_multicast", "is_unspecified"}
)


def _public_range_accesses(nodes: list[tuple[ast.AST, ast.AST]]) -> int:
    count = 0
    for owner_tree, node in nodes:
        for child in local_subtree(node):
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
        for call in local_subtree(node)
        if isinstance(call, ast.Call)
    )
    return parses, _public_range_accesses(nodes)


@_module_aware
def public_target_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    offenders = []
    for name, node, nodes in _property_sites(tree, path, prop, modules=_PROPERTY_MODULES.get()):
        parses, ranges = _public_target_counts(nodes)
        if parses and ranges:
            offenders.append(
                offender(path, getattr(node, "lineno", 1), f"{name} reimplements public-address policy")
            )
    return offenders


def test_public_target_has_one_implementation():
    prop = _property("public-target")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(public_target_offenders(tree, path, prop))
    assert _observed_property_count(prop), "public-target detector observed no fingerprint occurrences"
    assert not offenders, "public-address policy has a second implementation: " + ", ".join(offenders)


def test_public_target_registry_is_not_empty():
    assert _property("public-target").fingerprints


def test_public_target_detector_quiet_on_runtime_computed_name():
    runtime_name = ast.parse(
        "import ipaddress\n"
        "def attribute_name():\n"
        "    return 'ip_address'\n"
        "def verdict(value):\n"
        "    name = attribute_name()\n"
        "    return not getattr(ipaddress, name)(value).is_private\n"
    )
    assert not public_target_offenders(
        runtime_name,
        APP_ROOT / "_synthetic.py",
        _property("public-target"),
    )


def _observe_public_target(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    if _relative_app_path(path) != prop.owner_module:
        walk = _PropertyObservations(tree, path, prop)
        for _name, _node, nodes in _property_sites(tree, path, prop, modules=None):
            parses, ranges = _public_target_counts(nodes)
            if parses and ranges:
                walk.record("ipaddress_range_verdict", min(parses, ranges))
        return walk.counts
    walk = _PropertyObservations(tree, path, prop)
    owner_ids = _owner_definition_ids(tree, path, prop)
    owner_nodes = [
        node
        for node in nodes_of(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and id(node) in owner_ids
    ]
    owner_parses, owner_ranges = _public_target_counts([(tree, node) for node in owner_nodes])
    if owner_parses and owner_ranges:
        walk.record("ipaddress_range_verdict", min(owner_parses, owner_ranges))
    return walk.counts


# The registry's observation dispatch: this module owns the public-target walk.
_OBSERVERS["public-target"] = _observe_public_target
