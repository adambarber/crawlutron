"""The bool-env registry: boolean parsing has one implementation."""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import app_trees, has_method_call, local_subtree, nodes_of, offender
from tests.invariants._registry_machinery import (
    _OBSERVERS,
    _PROPERTY_MODULES,
    _module_aware,
    _observed_property_count,
    _owner_definition_ids,
    _property,
    _property_nodes,
    _relative_app_path,
    _resolvedlocal_nodes,
)
from tests.property_registry import Property

_BOOL_ENV_SPELLINGS = frozenset({"0", "1", "false", "true", "no", "yes", "off", "on"})


# `getattr(raw, "strip")` counts too: it reaches the same method, and recognizing only the attribute form
# would leave a one-token rewrite able to hide the comparison.
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
    descendants = [child for _owner_tree, node in nodes for child in local_subtree(node)]
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
    local_nodes_with_tree = [
        (owner_tree, child) for owner_tree, node in nodes for child in local_subtree(node)
    ]
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
            for part in local_subtree(child)
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
    tree_sets = _bool_env_tree_sets(tree, None)
    contexts = _bool_env_contexts(tree, path, prop, None)
    observations = [_bool_env_node_observation(context, tree_sets) for context in contexts]
    if not any(observed for observed, _count in observations):
        return {}
    return {"bool_literal_set": sum(count for _observed, count in observations)}


@_module_aware
def bool_env_offenders(tree: ast.AST, path: pathlib.Path, prop: Property) -> list[str]:
    tree_sets = _bool_env_tree_sets(tree, _PROPERTY_MODULES.get())
    excluded = _owner_definition_ids(tree, path, prop)
    return [
        offender(path, getattr(node, "lineno", 1), f"{name} reimplements boolean environment parsing")
        for name, node in _property_nodes(tree, path, prop)
        if _bool_env_node_observation(
            _resolvedlocal_nodes(tree, node, excluded, modules=_PROPERTY_MODULES.get(), prop=prop), tree_sets
        )[1]
    ]


def test_bool_env_has_one_implementation():
    prop = _property("bool-env")
    offenders: list[str] = []
    for path, tree in app_trees(exclude=frozenset()):
        offenders.extend(bool_env_offenders(tree, path, prop))
    assert _observed_property_count(prop), "bool-env detector observed no fingerprint occurrences"
    assert not offenders, "boolean environment policy has a second implementation: " + ", ".join(offenders)


def test_bool_env_registry_is_not_empty():
    assert _property("bool-env").fingerprints


# The registry's observation dispatch: this module owns the bool-env walk, the
# machinery owns the lookup; registering keeps the import graph one-directional.
_OBSERVERS["bool-env"] = _bool_env_observations
