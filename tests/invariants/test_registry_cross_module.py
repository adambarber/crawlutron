"""Registry checks that span modules: detectors resolve imported helpers."""

from __future__ import annotations

import ast

from tests.chokepoint_walks import REPO_ROOT, nodes_of
from tests.invariants._registry_machinery import (
    _owner_definition_ids,
    _property,
    _property_owner,
    _property_owner_failures,
)
from tests.invariants._shared import assert_property_detector_bites
from tests.invariants.test_registry_bool_env import bool_env_offenders
from tests.invariants.test_registry_robots import robots_policy_offenders
from tests.invariants.test_registry_targets import public_target_offenders


def test_public_target_detector_resolves_imported_helpers():
    assert_property_detector_bites(
        _property("public-target"),
        public_target_offenders,
        caught={
            "imported verdict helper": (
                {
                    "crawlutron/_public_helper.py": (
                        "import ipaddress\n"
                        "def parse_address(value):\n"
                        "    return ipaddress.ip_address(value)\n"
                    ),
                    "crawlutron/_public_consumer.py": (
                        "from crawlutron._public_helper import parse_address\n"
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
                "crawlutron/_public_owner.py": "def verdict(value):\n    return value\n",
                "crawlutron/_public_consumer.py": (
                    "from crawlutron._public_owner import verdict\n"
                    "def check(value):\n    return verdict(value)\n"
                ),
            },
        },
    )


def test_bool_env_detector_resolves_imported_helpers():
    assert_property_detector_bites(
        _property("bool-env"),
        bool_env_offenders,
        caught={
            "imported normalization helper": (
                {
                    "crawlutron/_bool_helper.py": (
                        "def normalize(value):\n    return value.strip().lower()\n"
                    ),
                    "crawlutron/_bool_consumer.py": (
                        "from crawlutron._bool_helper import normalize\n"
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
                "crawlutron/_bool_owner.py": "def parse(value):\n    return value\n",
                "crawlutron/_bool_consumer.py": (
                    "from crawlutron._bool_owner import parse\ndef check(value):\n    return parse(value)\n"
                ),
            },
        },
    )


def test_robots_policy_detector_resolves_imported_helpers():
    assert_property_detector_bites(
        _property("robots-policy"),
        robots_policy_offenders,
        caught={
            "imported policy helper": (
                {
                    "crawlutron/_robots_helper.py": (
                        "def parse_robot_policy(text):\n"
                        "    return [(True, text)]\n"
                        "def load_policy(text):\n"
                        "    return parse_robot_policy(text)\n"
                    ),
                    "crawlutron/_robots_consumer.py": (
                        "from crawlutron._robots_helper import load_policy\n"
                        "def load(text):\n"
                        "    return load_policy(text)\n"
                    ),
                },
                1,
            ),
        },
        quiet={
            "shared field parser": {
                "crawlutron/_robots_primitive.py": (
                    "def parse_fields(text):\n    return [(True, line) for line in text.splitlines()]\n"
                ),
                "crawlutron/_robots_consumer.py": (
                    "from crawlutron._robots_primitive import parse_fields\n"
                    "def load(text):\n"
                    "    return parse_fields(text)\n"
                ),
            },
        },
    )


#: A module that defines ``parse_bool_value`` twice -- the shape both registry proofs below are about.
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
    definitions = {
        "parse_bool_value": [("crawlutron/config/env.py", node) for node in _duplicate_def_nodes(tree)]
    }
    assert _property_owner_failures((prop,), definitions) == ["parse_bool_value=2"]


def test_property_owner_duplicate_is_not_sanctioned():
    prop = _property("bool-env")
    tree = ast.parse(DUPLICATE_DEF_SOURCE)
    path = REPO_ROOT / "crawlutron" / "config" / "env.py"
    owner_ids = _owner_definition_ids(tree, path, prop)
    duplicate = _duplicate_def_nodes(tree)[1]
    assert len(owner_ids) == 1, (
        "a duplicate def must not register a second owner id; the owner set is "
        f"identity-keyed, so both nodes of one shape collapse to one: got {len(owner_ids)}"
    )
    assert not _property_owner(path, prop, tree=tree, node=duplicate)
    assert bool_env_offenders(tree, path, prop)
