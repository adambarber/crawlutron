"""The robots-policy registry: one shared parser, one policy."""

from __future__ import annotations

import ast
import pathlib
from functools import cache

from tests.chokepoint_walks import (
    app_trees,
    has_method_call,
    local_call_leaf_names,
    local_nodes,
    local_subtree,
    offender,
    resolved_call_name,
)
from tests.invariants._shared import _relative_app_path, assert_property_detector_bites
from tests.invariants.test_registry import (
    _PROPERTY_MODULES,
    _module_aware,
    _observed_property_count,
    _owner_definition_ids,
    _property,
    _property_contexts,
    _property_nodes,
    _property_suppressed,
    _PropertyObservations,
    _resolvedlocal_nodes,
)
from tests.property_registry import Property


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


# `"{}/robots.txt".format(base)` and `"/robots.txt".join(...)` build a URL the same way a BinOp or f-string
# does, so the URL literal detector must see them.
_STRING_CONSTRUCTOR_METHODS = frozenset({"format", "join"})


_URL_CONSTRUCTOR_METHODS = frozenset({"resolve"})


# How a line-oriented robots file gets torn into lines. A hand-rolled scanner built on anything else is the
# duplicated robots parser this detector bans.
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
    if _relative_app_path(path) == "app/orchestration/robots_parse.py" and (
        "robot" in name or "parse" in name
    ):
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
                "from app.safety.bounded_ttl import BoundedTTLCache as Cache\n"
                "from app.orchestration.robots_parse import parse_robots_rules as parse\n"
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
            "from app.orchestration.robots import load_robots_rules as load\n"
            "def check(origin):\n"
            "    return load(origin, fetcher, 5)\n"
        ),
    )


def _observe_robots_policy(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    walk = _PropertyObservations(tree, path, prop)
    contexts = (
        _property_contexts(tree)
        if _relative_app_path(path) == prop.owner_module
        else [node for _name, node in _property_nodes(tree, path, prop)]
    )
    module_paths = _robot_module_paths(tree, path, None)
    for node in contexts:
        resolved = _resolvedlocal_nodes(tree, node, modules=None, prop=prop)
        for fingerprint, count in _robot_capabilities_from_nodes(
            tree, path, prop, resolved, module_paths, suppress_shared=False
        ).items():
            walk.record(fingerprint, count)
    return walk.counts
