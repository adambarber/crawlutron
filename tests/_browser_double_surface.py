"""The browser-double protocol surface census: production members vs fake members.

Backs the harness-doubles browser extension (#844): both sides read from the
live AST, so a member dropped on either end is a finding, not a silent green.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import APP_ROOT, REPO_ROOT

_PROTOCOL_HOME = APP_ROOT / "transport" / "render_page.py"
_PAGE_DOUBLE_HOME = REPO_ROOT / "tests" / "_browser_page_doubles.py"


def _member_names(node: ast.ClassDef) -> frozenset[str]:
    """The members one class body declares: annotations, defs, and the ``self.X``
    attributes ``__init__`` establishes -- a fake's ``self.url = ...`` is the
    same declaration a Protocol's ``url: str`` annotation is."""
    names: set[str] = set()
    for stmt in node.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.AnnAssign)):
            if isinstance(stmt, ast.AnnAssign):
                if not isinstance(stmt.target, ast.Name):
                    continue
                name = stmt.target.id
            else:
                name = stmt.name
            if not name.startswith("_"):
                names.add(name)
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "__init__":
                for inner in ast.walk(stmt):
                    if (
                        isinstance(inner, ast.Assign)
                        and isinstance(inner.targets[0], ast.Attribute)
                        and isinstance(inner.targets[0].value, ast.Name)
                        and inner.targets[0].value.id == "self"
                        and not inner.targets[0].attr.startswith("_")
                    ):
                        names.add(inner.targets[0].attr)
    return frozenset(names)


def _protocol_members() -> dict[str, frozenset[str]]:
    """Every Protocol class in render_page.py, mapped to its member set."""
    out: dict[str, frozenset[str]] = {}
    for node in ast.parse(_PROTOCOL_HOME.read_text(encoding="utf-8")).body:
        if isinstance(node, ast.ClassDef) and any(
            (isinstance(b, ast.Name) and b.id == "Protocol") for b in node.bases
        ):
            out[node.name] = _member_names(node)
    return out


def _page_double_classes(home: pathlib.Path = _PAGE_DOUBLE_HOME) -> dict[str, ast.ClassDef]:
    """The page-double module's classes, by name; `home` is injectable so the
    mutation probe can drive a mutated tree without touching the workspace."""
    tree = ast.parse(home.read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, ast.ClassDef)}


def _standin_members(fake: str, home: pathlib.Path = _PAGE_DOUBLE_HOME) -> frozenset[str]:
    """The members `fake` carries, including everything it inherits from its base."""
    classes = _page_double_classes(home)
    if fake not in classes:
        return frozenset()
    seen: set[str] = set()
    stack = [classes[fake]]
    while stack:
        node = stack.pop()
        seen |= _member_names(node)
        for base in node.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                stack.append(classes[base.id])
    return frozenset(seen)
