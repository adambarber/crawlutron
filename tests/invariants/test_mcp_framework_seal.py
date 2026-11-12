"""The MCP framework-freeness seal: no module under ``crawlutron.mcp`` imports fastapi or starlette. The AST
scan catches the import however spelled; attribute-form use requires the import first, so the import scan
is the reach chokepoint.
"""

from __future__ import annotations

import ast
import pathlib

_MCP_ROOT = pathlib.Path(__file__).resolve().parents[2] / "crawlutron" / "mcp"
_FORBIDDEN_ROOTS = ("fastapi", "starlette")


def _imported_roots(tree: ast.AST) -> set[str]:
    """Top-level roots of every import in the tree, alias-proof."""
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _mcp_sources() -> dict[str, ast.AST]:
    return {
        str(path.relative_to(_MCP_ROOT)): ast.parse(path.read_text(encoding="utf-8"))
        for path in sorted(_MCP_ROOT.rglob("*.py"))
    }


def test_no_mcp_module_imports_fastapi_or_starlette() -> None:
    """The seal: any mcp module importing a forbidden root is named file+line."""
    offenders: list[str] = []
    for name, tree in _mcp_sources().items():
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            root = (
                node.names[0].name.split(".")[0]
                if isinstance(node, ast.Import)
                else (node.module or "").split(".")[0]
            )
            if root in _FORBIDDEN_ROOTS:
                offenders.append(f"crawlutron/mcp/{name}:{node.lineno} imports {ast.unparse(node)}")
    assert not offenders, (
        "crawlutron.mcp imports fastapi/starlette; the MCP protocol layer is "
        "framework-free so it can embed in any ASGI server:\n" + "\n".join(offenders)
    )


def test_seal_catches_aliased_and_nested_import_shapes() -> None:
    """Alias and submodule spellings are the same reach: a planted module reds
    whichever form it uses."""
    forms = [
        "from fastapi import HTTPException as HX",
        "import fastapi.responses",
        "from starlette.middleware import base",
        "import starlette as st",
    ]
    for form in forms:
        roots = _imported_roots(ast.parse(form))
        assert roots & set(_FORBIDDEN_ROOTS), f"the seal missed the planted form: {form}"
