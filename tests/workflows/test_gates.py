"""The suite's own architecture is enforced, not merely documented."""

from __future__ import annotations

import ast
from pathlib import Path

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
        if isinstance(node, ast.Call):
            names = {node.func.id} if isinstance(node.func, ast.Name) else set()
            if isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
            literals = {
                arg.value for arg in node.args if isinstance(arg, ast.Constant) and isinstance(arg.value, str)
            }
            if names & {"getattr", "__import__", "import_module"} and (
                "yaml" in literals or literals & _PARSERS
            ):
                found.append((node.lineno, "dynamic yaml access"))
    return found


def _parse_door_calls(source: str) -> set[str]:
    tree = ast.parse(source)
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }


def _cast_calls(source: str) -> list[int]:
    tree = ast.parse(source)
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "cast"
    ]


def test_the_parse_door_actually_parses() -> None:
    assert {"get_single_node", "construct_document"} <= _parse_door_calls(
        (_SUITE / "_model.py").read_text(encoding="utf-8")
    )


def test_workflow_yaml_is_parsed_in_exactly_one_module() -> None:
    for path in _files():
        violations = _yaml_violations(path)
        assert not violations, f"{path}: {violations}"


def test_the_workflow_suite_has_no_cast_soup() -> None:
    for path in _files():
        lines = _cast_calls(path.read_text(encoding="utf-8"))
        assert not lines, f"{path}: {lines}: cast() hides a YAML shape"


def test_the_yaml_gate_bites_an_aliased_import(tmp_path: Path) -> None:
    path = tmp_path / "second_parse.py"
    path.write_text("import yaml as y\nraw = y.safe_load('a: 1')\n", encoding="utf-8")
    assert _yaml_violations(path)


def test_the_yaml_gate_bites_a_full_loader(tmp_path: Path) -> None:
    path = tmp_path / "loader.py"
    path.write_text("import yaml\nraw = yaml.FullLoader('a: 1')\n", encoding="utf-8")
    assert _yaml_violations(path)


def test_the_yaml_gate_bites_dynamic_access(tmp_path: Path) -> None:
    path = tmp_path / "dynamic.py"
    path.write_text("import yaml as y\nraw = getattr(y, 'safe_load')('a: 1')\n", encoding="utf-8")
    assert _yaml_violations(path)


def test_the_cast_gate_bites_a_cast() -> None:
    assert _cast_calls("from typing import cast\nx = cast(dict, y)\n")


def test_the_parse_door_gate_bites_its_removal() -> None:
    source = "root = loader.get_single_node()\nraw = loader.construct_document(root)\n"
    assert {"get_single_node", "construct_document"} <= _parse_door_calls(source)
    assert "get_single_node" not in _parse_door_calls("raw = loader.construct_document(None)\n")
