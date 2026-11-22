"""Path roots, labels and composition come from one door, never restated."""

import ast
import os
import pathlib
import subprocess
import sys
from types import MappingProxyType

import pytest

import utils.paths as path_door
from tests._walk_primitives import SCAN_ROOTS, member_src_roots
from tests.chokepoint_paths import REPO_ROOT
from tests.chokepoint_walks import nodes_of, offender_at, parsed_source, trees_under

_OWNED = frozenset(
    {
        "ADMISSION_ROOT",
        "APP_ROOT",
        "APP_ROOTS",
        "MCP_ROOT",
        "REST_ROOT",
        "REPO_ROOT",
        "SAFE_URL_ROOT",
        "TESTS_DIR",
        "TRANSPORT_DIR",
        "path_components",
        "relative_posix",
    }
)
_PATH_OWNER = "utils/paths.py"
_PATH_DOORS = frozenset({"tests/chokepoint_paths.py", "tests/_walk_primitives.py", _PATH_OWNER})
_ALLOWED_DOORS = frozenset({"tests.chokepoint_paths", "utils.paths"})

# Production modules locating their own sibling data are not restating the repo layout.
_PRODUCTION_SRC = tuple(r.relative_to(REPO_ROOT).as_posix() for r in member_src_roots())


def _file_anchor(node: ast.expr) -> bool:
    """True for ``Path(__file__)`` in either call spelling."""
    if not isinstance(node, ast.Call) or not node.args:
        return False
    func = node.func
    named = (isinstance(func, ast.Name) and func.id == "Path") or (
        isinstance(func, ast.Attribute) and func.attr == "Path"
    )
    arg = node.args[0]
    return named and isinstance(arg, ast.Name) and arg.id == "__file__"


def _bottom(node: ast.expr) -> bool:
    """Walk a path chain past ``resolve()`` down to ``Path(__file__)``."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "resolve":
        node = node.func.value
    return _file_anchor(node)


def _parent_hops(node: ast.expr) -> int:
    """Parent hops in a module-file derivation, or zero if it is not one."""
    if (
        isinstance(node, ast.Subscript)
        and isinstance(node.value, ast.Attribute)
        and node.value.attr == "parents"
        and isinstance(node.slice, ast.Constant)
        and isinstance(node.slice.value, int)
        and _bottom(node.value.value)
    ):
        return node.slice.value + 1
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        hops = 0
        cursor: ast.expr = node
        while isinstance(cursor, ast.Attribute) and cursor.attr == "parent":
            hops += 1
            cursor = cursor.value
        return hops if _bottom(cursor) else 0
    return 0


def _ad_hoc_nodes(tree: ast.Module, path: pathlib.Path) -> list[ast.AST]:
    """Derivations that land on the repo root: layout code, not sibling lookups."""
    depth = len(path.parent.relative_to(REPO_ROOT).parts)
    return [
        node
        for node in nodes_of(tree)
        if depth and isinstance(node, ast.expr) and _parent_hops(node) == depth + 1
    ]


def _ad_hoc(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Name every ad hoc repo-root derivation file:line."""
    return [
        offender_at(path, node, "derive the repo root from tests.chokepoint_paths, not __file__")
        for node in _ad_hoc_nodes(tree, path)
    ]


def _wrong_door_nodes(tree: ast.Module, path: pathlib.Path) -> list[tuple[ast.ImportFrom, ast.alias]]:
    """Owned names imported from anywhere but the one door."""
    return [
        (node, alias)
        for node in nodes_of(tree)
        if isinstance(node, ast.ImportFrom) and node.module not in _ALLOWED_DOORS
        for alias in node.names
        if alias.name in _OWNED
    ]


def _wrong_door(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Name every wrong-door path import file:line."""
    return [
        offender_at(path, node, f"{alias.name} must be imported from tests.chokepoint_paths")
        for node, alias in _wrong_door_nodes(tree, path)
    ]


def _local_parser_nodes(tree: ast.Module, path: pathlib.Path) -> list[ast.AST]:
    """Second path-composition parsers outside the one owner."""
    return [
        node
        for node in nodes_of(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name in {"path_components", "_path_components"}
    ]


def _local_parser(tree: ast.Module, path: pathlib.Path) -> list[str]:
    """Name every local path parser file:line."""
    return [
        offender_at(path, node, "path composition lives in tests.chokepoint_paths")
        for node in _local_parser_nodes(tree, path)
    ]


def _sweep(detector, *, infrastructure_only: bool = False) -> list[str]:
    """One detector over every python-bearing root, owner and door excluded.

    Infrastructure-only drops production src: a module finding its own data
    file is not restating the repository layout, and cannot import test code.
    """
    out: list[str] = []
    for root in SCAN_ROOTS:
        for path, tree in trees_under(REPO_ROOT / root):
            relative = path.relative_to(REPO_ROOT).as_posix()
            if relative in _PATH_DOORS:
                continue
            if infrastructure_only and relative.startswith(_PRODUCTION_SRC):
                continue
            out.extend(detector(tree, path))
    return out


def test_ad_hoc_root_derivation_is_detected() -> None:
    probe = parsed_source("ROOT = Path(__file__).resolve().parents[2]\n")
    found = _ad_hoc(probe, REPO_ROOT / "tests" / "invariants" / "probe.py")
    assert len(found) == 1
    assert found[0].startswith("tests/invariants/probe.py:1 ")


def test_wrong_door_import_is_detected() -> None:
    probe = parsed_source("from tests._walk_primitives import REPO_ROOT\n")
    assert len(_wrong_door(probe, REPO_ROOT / "probe.py")) == 1


def test_local_parser_is_detected() -> None:
    probe = parsed_source("def path_components(expr):\n    return [expr]\n")
    assert len(_local_parser(probe, REPO_ROOT / "probe.py")) == 1


def test_detectors_stay_clean_on_a_conforming_module() -> None:
    probe = parsed_source("from tests.chokepoint_paths import REPO_ROOT\nROOT = REPO_ROOT\n")
    assert not any(d(probe, REPO_ROOT / "probe.py") for d in (_ad_hoc, _wrong_door, _local_parser))


def test_the_door_freezes_its_names() -> None:
    with pytest.raises(AttributeError, match="immutable"):
        path_door.REPO_ROOT = pathlib.Path("elsewhere")


def test_derived_names_are_cached_once_and_immutable() -> None:
    roots = path_door.SCAN_ROOTS
    misses = path_door._surface.cache_info().misses
    assert path_door.SCAN_ROOTS is roots
    assert path_door._surface.cache_info().misses == misses
    assert isinstance(roots, tuple)
    assert isinstance(path_door.workspace_package_dirs(path_door.REPO_ROOT), MappingProxyType)


def test_no_ad_hoc_root_derivation_survives() -> None:
    found = _sweep(_ad_hoc, infrastructure_only=True)
    assert not found, f"ad hoc roots: {found[:8]}"


def test_every_path_symbol_comes_from_the_one_door() -> None:
    assert not _sweep(_wrong_door), f"wrong doors: {_sweep(_wrong_door)[:8]}"


def test_path_composition_has_one_home() -> None:
    assert not _sweep(_local_parser), f"local parsers: {_sweep(_local_parser)[:8]}"


def test_root_is_independent_of_the_callers_cwd(tmp_path: pathlib.Path) -> None:
    """The door is installed at one canonical place: importing it from a foreign cwd
    must still name this tree, or a caller can silently redirect every gate."""
    probe = "from utils.paths import REPO_ROOT\nprint(REPO_ROOT)\n"
    run = subprocess.run(
        [sys.executable, "-c", probe],
        env=dict(os.environ, PYTHONPATH=str(REPO_ROOT)),
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert pathlib.Path(run.stdout.strip()) == REPO_ROOT
