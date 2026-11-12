from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, app_trees, local_nodes, nodes_of, resolved_call_name
from tests.invariants._shared import DefinitionNode, _module_definition_index, _module_name

_DECISION_NAMES = frozenset(
    {
        # The constructor is the admission chokepoint since the value object took over from the egress
        # helpers: calling it IS the decision.
        "NormalizedUrl",
        "_validated_url",
        "admit_or_none",
        "checked_payload_urls",
        "filter_js_endpoints",
        "validate_webhook_url_async",
    }
)
_MODULE_PREFIXES = ("crawlutron_rest.api.", "crawlutron.mcp.", "crawlutron.service.")


def _call_edges(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...],
    index: dict[str, list[tuple[pathlib.Path, ast.Module, DefinitionNode]]],
) -> dict[str, set[str]]:
    def same_tree_target(
        tree: ast.Module, callee: str
    ) -> list[tuple[pathlib.Path, ast.Module, DefinitionNode]]:
        # The same-tree fallback for a bare-name call the import resolver cannot
        # see: one module-local definition with that name, or nothing.
        matches = [
            (candidate_name, candidate)
            for candidate_name, candidate in index.items()
            if len(candidate) == 1
            and candidate[0][1] is tree
            and getattr(candidate[0][2], "name", None) == callee
        ]
        if len(matches) == 1:
            return matches[0][1]
        return []

    edges: dict[str, set[str]] = {name: set() for name in index}
    for name, candidates in index.items():
        if len(candidates) != 1:
            continue
        _path, tree, node = candidates[0]
        for child in local_nodes(node):
            if not isinstance(child, ast.Call):
                continue
            resolved = resolved_call_name(tree, child)
            target = index.get(resolved, [])
            if not target and isinstance(child.func, ast.Name):
                target = same_tree_target(tree, child.func.id)
            offloaded = resolved == "asyncio.to_thread" and child.args and isinstance(child.args[0], ast.Name)
            if not target and offloaded:
                # asyncio.to_thread(f, ...) calls f: the offload convention is a
                # real call edge the plain resolver cannot see on its own.
                first_arg = child.args[0]
                assert isinstance(first_arg, ast.Name)
                target = same_tree_target(tree, first_arg.id)
            if len(target) == 1 and target[0][2] is not node:
                target_name = next(
                    candidate_name
                    for candidate_name, candidate in index.items()
                    if len(candidate) == 1 and candidate[0] is target[0]
                )
                edges[name].add(target_name)
    return edges


def _mcp_handlers(tree: ast.Module) -> set[str]:
    handlers: set[str] = set()
    for node in nodes_of(tree):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "handler" and isinstance(keyword.value, ast.Name):
                handlers.add(keyword.value.id)
    return handlers


def _has_policy_reference(tree: ast.Module, node: ast.AST) -> bool:
    for child in nodes_of(node):
        if isinstance(child, ast.Name) and child.id == "HostPolicy":
            return True
        if isinstance(child, ast.Attribute) and child.attr == "HostPolicy":
            return True
        if (
            isinstance(child, ast.Call)
            and resolved_call_name(tree, child).rsplit(".", 1)[-1] in _DECISION_NAMES
        ):
            return True
    return False


def discover_host_policy_entry_points(
    modules: tuple[tuple[pathlib.Path, ast.Module], ...],
) -> set[str]:
    index = _module_definition_index(modules)
    edges = _call_edges(modules, index)
    reverse: dict[str, set[str]] = {name: set() for name in index}
    for caller, callees in edges.items():
        for callee in callees:
            reverse[callee].add(caller)
    reachable: set[str] = set()
    pending = [name for name in index if name.rsplit(".", 1)[-1] in _DECISION_NAMES]
    while pending:
        name = pending.pop()
        if name in reachable:
            continue
        reachable.add(name)
        pending.extend(reverse.get(name, ()))
    discovered: set[str] = set()
    for name, candidates in index.items():
        if len(candidates) != 1:
            continue
        path, tree, node = candidates[0]
        module = _module_name(path)
        if (
            not module.startswith(_MODULE_PREFIXES)
            or name in reachable
            and name.rsplit(".", 1)[-1] in _DECISION_NAMES
        ):
            continue
        leaf = name.rsplit(".", 1)[-1]
        handler_names = _mcp_handlers(tree)
        if leaf.startswith("_") and leaf not in handler_names:
            continue
        if leaf not in handler_names and not _has_policy_reference(tree, node):
            continue
        if name in reachable or leaf in handler_names:
            discovered.add(f"{path.relative_to(REPO_ROOT).as_posix()}:{leaf}")
    return discovered


def _app_modules() -> tuple[tuple[pathlib.Path, ast.Module], ...]:
    return app_trees(exclude=frozenset())


def _table_entries() -> set[str]:
    from tests._host_policy_entry_table import host_policy_entry_table

    return host_policy_entry_table()


def test_host_policy_entry_table_matches_ast_discovery():
    assert discover_host_policy_entry_points(_app_modules()) == _table_entries()


def test_host_policy_discovery_rejects_undeclared_entry_point():
    modules = _app_modules() + (
        (
            REPO_ROOT / "packages/rest/crawlutron_rest/api/_undeclared_entry.py",
            ast.parse(
                "from crawlutron.models.host_policy import HostPolicy\n"
                "from crawlutron.models.normalized_url import admit_or_none\n"
                "def undeclared_entry(url, *, policy: HostPolicy):\n"
                "    return admit_or_none(url, policy=policy)\n"
            ),
        ),
    )
    discovered = discover_host_policy_entry_points(modules)
    assert "packages/rest/crawlutron_rest/api/_undeclared_entry.py:undeclared_entry" in discovered
    assert discovered - _table_entries()
