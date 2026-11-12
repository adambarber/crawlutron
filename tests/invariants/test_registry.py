"""The property registry keeps owners real and detectors honest. The shared machinery (resolution, owner
keys, observation plumbing) lives in ``tests.invariants._registry_machinery``; this module holds the
registry's own tests and the cross-detector acceptance those tests pin.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import app_trees, nodes_of
from tests.invariants._registry_machinery import (
    _property,
    _property_owner_failures,
    _relative_app_path,
)
from tests.property_registry import PROPERTY_REGISTRY, Property

_REQUIRED_PROPERTY_FINGERPRINTS = {
    "public-target": frozenset({"ipaddress_range_verdict"}),
    "bool-env": frozenset({"bool_literal_set"}),
    "robots-policy": frozenset(
        {"robots_url", "parse_robots_rules_call", "parse_robots_rules_definition", "robots_bounded_ttl_cache"}
    ),
}


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
            from tests.invariants._registry_machinery import _property_observations

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


def test_property_registry_is_not_empty():
    assert PROPERTY_REGISTRY


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
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
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
