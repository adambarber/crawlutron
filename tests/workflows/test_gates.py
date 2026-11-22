"""The suite's own architecture is enforced, not merely documented."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_SUITE = Path(__file__).parent
_PARSERS = frozenset(
    {
        "load",
        "safe_load",
        "compose",
        "full_load",
        "unsafe_load",
        "load_all",
        "safe_load_all",
        "compose_all",
        "full_load_all",
        "unsafe_load_all",
        "SafeLoader",
        "CSafeLoader",
        "FullLoader",
        "BaseLoader",
        "UnsafeLoader",
    }
)


def _files():
    return sorted(path for path in _SUITE.rglob("*.py") if path.is_file())


def _yaml_violations(path: Path) -> list[tuple[int, str]]:
    if path.name == "_model.py":
        return []
    tree = ast.parse(path.read_text(encoding="utf-8"))
    aliases = {"yaml"}
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(item.asname for item in node.names if item.name == "yaml" and item.asname)
        if isinstance(node, ast.ImportFrom) and node.module == "yaml":
            found.append((node.lineno, "yaml import"))
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in aliases
            and node.attr in _PARSERS
        ):
            found.append((node.lineno, f"yaml.{node.attr}"))
    return found


def test_the_parse_door_actually_parses() -> None:
    tree = ast.parse((_SUITE / "_model.py").read_text(encoding="utf-8"))
    calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert {"get_single_node", "construct_document"} <= calls


def test_workflow_yaml_is_parsed_in_exactly_one_module() -> None:
    for path in _files():
        violations = _yaml_violations(path)
        assert not violations, f"{path}: {violations}"


def test_the_workflow_suite_has_no_cast_soup() -> None:
    for path in _files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cast":
                pytest.fail(f"{path}:{node.lineno}: cast() hides a YAML shape")


def test_the_yaml_gate_bites_an_aliased_import(tmp_path: Path) -> None:
    path = tmp_path / "second_parse.py"
    path.write_text("import yaml as y\nraw = y.safe_load('a: 1')\n", encoding="utf-8")
    assert _yaml_violations(path)


def test_the_yaml_gate_bites_a_full_loader(tmp_path: Path) -> None:
    path = tmp_path / "loader.py"
    path.write_text("import yaml\nraw = yaml.FullLoader('a: 1')\n", encoding="utf-8")
    assert _yaml_violations(path)
