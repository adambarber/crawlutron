"""The public-target registry: one implementation, observed everywhere."""

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT, app_trees, local_nodes, nodes_of, offender, resolved_call_name
from tests.invariants._shared import _relative_app_path, assert_property_detector_bites
from tests.invariants.test_registry import (
    _PROPERTY_MODULES,
    _module_aware,
    _observed_property_count,
    _owner_definition_ids,
    _property,
    _property_sites,
    _PropertyObservations,
)
from tests.property_registry import Property

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
            "from app.safety.validation import _is_public_ip as verdict\n"
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


def _observe_public_target(tree: ast.AST, path: pathlib.Path, prop: Property) -> dict[str, int]:
    walk = _PropertyObservations(tree, path, prop)
    if _relative_app_path(path) == prop.owner_module:
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
    for _name, _node, nodes in _property_sites(tree, path, prop, modules=None):
        parses, ranges = _public_target_counts(nodes)
        if parses and ranges:
            walk.record("ipaddress_range_verdict", min(parses, ranges))
    return walk.counts
