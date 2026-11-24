"""URL assertions have one door: tests._host_assert, never substring checks."""

from __future__ import annotations

import ast
import pathlib

from tests._host_assert import is_host_literal
from tests._walk_primitives import scan_findings


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


def _url_findings(path: pathlib.Path) -> list[tuple[int, str]]:
    """Unsafe assertion lines as (line, detail) for the one shared finding door."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return [(line, "unsafe URL substring assertion") for line in _offending_lines(tree)]


def test_no_unsafe_url_substring_assertion() -> None:
    """One loop reports every file:line substring assertion, so no failure hides another."""
    failures = scan_findings(_url_findings)
    assert not failures, (
        "unsafe URL substring assertion; use tests._host_assert.host_of/host_in/host_in_source:\n"
        + "\n".join(failures)
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
