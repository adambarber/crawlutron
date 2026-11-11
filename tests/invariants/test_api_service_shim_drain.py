"""The api→service shim drain is complete: zero re-export shims remain.

The rearchitecture spine moved every core module to ``crawlutron/service``; the
narrow re-export shims under ``crawlutron/api`` are dead surface the moment
their last consumer repoints. This gate holds the tree at zero shims -- a
re-added shim (or any new ``crawlutron/api`` module re-exporting a
``crawlutron.service`` module) is named file:line, never silently absorbed.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import REPO_ROOT

__all__ = ["_api_shim_offenders"]

_API_ROOT = REPO_ROOT / "crawlutron" / "api"

_SHIM_DOCSTRING_MARKERS = ("re-export shim", "Narrow re-export")


def _is_service_reexport_shim(tree: ast.Module) -> bool:
    """True when a crawlutron/api module is a pure re-export of crawlutron.service."""
    docstring = ast.get_docstring(tree) or ""
    for marker in _SHIM_DOCSTRING_MARKERS:
        if marker in docstring:
            return True
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)]
    if not imports or not all(
        n.module == "crawlutron.service" or (n.module or "").startswith("crawlutron.service.")
        for n in imports
    ):
        return False
    bodies = [n for n in tree.body if not isinstance(n, (ast.ImportFrom, ast.Import, ast.Expr))]
    return not bodies


def _api_shim_offenders(root: pathlib.Path = _API_ROOT) -> list[str]:
    def _named(path: pathlib.Path) -> str:
        return (
            path.relative_to(REPO_ROOT).as_posix() if root == _API_ROOT else path.relative_to(root).as_posix()
        )

    return [
        f"{_named(path)}: api re-export shim of crawlutron.service"
        for path in sorted(root.rglob("*.py"))
        if _is_service_reexport_shim(ast.parse(path.read_text(encoding="utf-8")))
    ]


def test_api_imports_no_service_reexport_shim() -> None:
    """A re-export shim under crawlutron/api is dead surface: it must not exist."""
    offenders = _api_shim_offenders()
    assert not offenders, "api shim drain incomplete (repoint consumers, delete the shim): " + "; ".join(
        offenders
    )


def test_shim_detector_bites(tmp_path: pathlib.Path) -> None:
    """The detector is not vacuous: a re-added shim is named, a real api module is not."""
    shim = tmp_path / "drain_probe_shim.py"
    shim.write_text(
        '"""Narrow re-export shim: the module moved to crawlutron/service."""\n\n'
        "from crawlutron.service.map_endpoint import map_site\n\n"
        '__all__ = ["map_site"]\n',
        encoding="utf-8",
    )
    real = tmp_path / "render.py"
    real.write_text(
        'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n__all__ = ["router"]\n',
        encoding="utf-8",
    )
    offenders = _api_shim_offenders(tmp_path)
    assert [name.split(":")[0] for name in offenders] == ["drain_probe_shim.py"], offenders
