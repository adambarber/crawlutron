"""The suite's own architecture is enforced, not merely documented."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SUITE = Path(__file__).parent


def _files():
    return sorted(path for path in _SUITE.rglob("*.py") if path.is_file())


def test_the_parse_door_actually_parses() -> None:
    tree = ast.parse((_SUITE / "_model.py").read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"get_single_node", "construct_document"} <= calls


def test_workflow_yaml_is_parsed_in_exactly_one_module() -> None:
    parsers = {"load", "safe_load", "compose", "SafeLoader", "CSafeLoader", "FullLoader"}
    for path in _files():
        if path.name == "_model.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and node.module == "yaml":
                pytest.fail(f"{path}:{node.lineno}: a yaml import outside the parse door")
            if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                if node.value.id == "yaml" and node.attr in parsers:
                    pytest.fail(f"{path}:{node.lineno}: yaml.{node.attr} outside the parse door")


def test_the_workflow_suite_has_no_cast_soup() -> None:
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cast":
                pytest.fail(f"{path}:{node.lineno}: cast() hides a YAML shape")
