"""The suite's own architecture is enforced, not merely documented."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SUITE = Path(__file__).parent


def _files():
    return sorted(path for path in _SUITE.rglob("*.py") if path.is_file())


def test_workflow_yaml_is_parsed_in_exactly_one_module() -> None:
    for path in _files():
        if path.name == "_model.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "safe_load":
                    pytest.fail(f"{path}:{node.lineno}: yaml.safe_load outside the parse door")


def test_the_workflow_suite_has_no_cast_soup() -> None:
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cast":
                pytest.fail(f"{path}:{node.lineno}: cast() hides a YAML shape")
