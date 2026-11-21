"""URL assertions have one door: tests._host_assert, never substring checks."""

from __future__ import annotations

import ast
import pathlib

import pytest

from tests._host_assert import is_host_literal
from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS


def _string_value(node: ast.AST) -> str | None:
    """The constant string a node denotes, or None for non-literals."""
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _is_host_text(value: str | None) -> bool:
    """True for a bare host literal, the substring-sanitization shape."""
    return value is not None and is_host_literal(value)


def _offending_lines(tree: ast.AST) -> list[int]:
    """Line numbers of asserts that test a host by substring or prefix."""
    lines: set[int] = set()
    for assert_node in ast.walk(tree):
        if not isinstance(assert_node, ast.Assert):
            continue
        for node in ast.walk(assert_node.test):
            if isinstance(node, ast.Compare):
                if _is_host_text(_string_value(node.left)) and any(
                    isinstance(op, (ast.In, ast.NotIn)) for op in node.ops
                ):
                    lines.add(node.lineno)
                for op, right in zip(node.ops, node.comparators, strict=False):
                    if isinstance(op, (ast.In, ast.NotIn)) and _is_host_text(_string_value(right)):
                        lines.add(node.lineno)
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"startswith", "endswith"}
                and any(_is_host_text(_string_value(arg)) for arg in node.args)
            ):
                lines.add(node.lineno)
    return sorted(lines)


def _scanned_files() -> list[pathlib.Path]:
    """Every Python file in the derived scan roots, sorted by repo-relative path."""
    return sorted(path for root in SCAN_ROOTS for path in (REPO_ROOT / root).rglob("*.py") if path.is_file())


@pytest.mark.parametrize("path", _scanned_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_no_unsafe_url_substring_assertion(path: pathlib.Path) -> None:
    """Every scanned file routes host comparisons through tests._host_assert."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = _offending_lines(tree)
    assert not offenders, (
        f"{path}: unsafe URL substring assertion at {offenders}; "
        "use tests._host_assert.host_of/host_in/host_in_source"
    )


def test_detector_flags_injected_substring_with_line_number() -> None:
    """Kills the mutant that drops comparison detection: a host literal in `in` must be named."""
    tree = ast.parse('value = "ghcr.io"\nassert "ghcr.io" in value\n')
    assert _offending_lines(tree) == [2]


def test_detector_flags_injected_prefix_with_line_number() -> None:
    """Kills the mutant that drops call detection: a host prefix assertion must be named."""
    tree = ast.parse('value = "example.com"\nassert value.startswith("example.com")\n')
    assert _offending_lines(tree) == [2]


def test_detector_accepts_the_sanctioned_helper() -> None:
    """Kills the mutant that flags the helper call itself, which would force an exemption."""
    tree = ast.parse('assert host_in(values, "ghcr.io")\n')
    assert not _offending_lines(tree)
