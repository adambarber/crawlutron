"""The api->service shim drain is complete: zero re-export shims remain. The rearchitecture spine moved every
core module to ``crawlutron/service``; the narrow re-export shims under ``crawlutron/api`` are dead
surface the moment their last consumer repoints.
"""

from __future__ import annotations

import ast
import pathlib

from tests.chokepoint_walks import APP_ROOT, REPO_ROOT

__all__ = ["_api_shim_offenders"]

_API_ROOT = APP_ROOT / "api"

_SHIM_DOCSTRING_MARKERS = ("re-export shim", "Narrow re-export")


def _is_all_assignment(node: ast.stmt) -> bool:
    """True for a plain ``__all__ = [...]`` statement (a shim's declaration, not its body)."""
    if isinstance(node, ast.Assign):
        return all(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets)
    return False


def _is_service_reexport_shim(tree: ast.Module) -> bool:
    """True when a crawlutron/api module is a pure re-export of crawlutron.service."""
    docstring = ast.get_docstring(tree) or ""
    for marker in _SHIM_DOCSTRING_MARKERS:
        if marker in docstring:
            return True
    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom) and (n.module or "") != "__future__"]
    if not imports or not all(
        n.module == "crawlutron.service" or (n.module or "").startswith("crawlutron.service.")
        for n in imports
    ):
        return False
    bodies = [
        n
        for n in tree.body
        if not isinstance(n, (ast.ImportFrom, ast.Import, ast.Expr)) and not _is_all_assignment(n)
    ]
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


def _write(root: pathlib.Path, name: str, source: str) -> pathlib.Path:
    """Write a probe module into a tmp root and return its path."""
    path = root / name
    path.write_text(source, encoding="utf-8")
    return path


def test_api_imports_no_service_reexport_shim() -> None:
    """A re-export shim under crawlutron/api is dead surface: it must not exist."""
    offenders = _api_shim_offenders()
    assert not offenders, "api shim drain incomplete (repoint consumers, delete the shim): " + "; ".join(
        offenders
    )


def test_shim_detector_bites(tmp_path: pathlib.Path) -> None:
    """The detector is not vacuous: a re-added shim is named, a real api module is not."""
    _write(
        tmp_path,
        "drain_probe_shim.py",
        '"""Narrow re-export shim: the module moved to crawlutron/service."""\n\n'
        "from crawlutron.service.map_endpoint import map_site\n\n"
        '__all__ = ["map_site"]\n',
    )
    _write(
        tmp_path,
        "render.py",
        'from fastapi import APIRouter\n\nrouter = APIRouter()\n\n__all__ = ["router"]\n',
    )
    offenders = _api_shim_offenders(tmp_path)
    assert [name.split(":")[0] for name in offenders] == ["drain_probe_shim.py"], offenders


def test_markerless_pure_reexport_shim_is_named(tmp_path: pathlib.Path) -> None:
    """A shim without the marker docstring (imports + __all__ + re-exports) is still named."""
    _write(
        tmp_path,
        "markerless_shim.py",
        "from __future__ import annotations\n\n"
        "from crawlutron.service.map_endpoint import map_site\n\n"
        '__all__ = ["map_site"]\n',
    )
    offenders = _api_shim_offenders(tmp_path)
    assert [name.split(":")[0] for name in offenders] == ["markerless_shim.py"], offenders


def test_markerless_non_shims_are_not_named(tmp_path: pathlib.Path) -> None:
    """Aliased imports and real functions keep a marker-less api module out of the gate."""
    _write(
        tmp_path,
        "aliased.py",
        'import crawlutron.service.map_endpoint as map_endpoint\n\n__all__ = ["map_endpoint"]\n',
    )
    _write(
        tmp_path,
        "real_fn.py",
        "from crawlutron.service.map_endpoint import map_site\n\n"
        "def render(url: str) -> str:\n    return map_site(url)\n\n"
        '__all__ = ["render"]\n',
    )
    assert _api_shim_offenders(tmp_path) == []
