"""The scan surface is derived by walking the repo, never hand-listed."""

from __future__ import annotations

import configparser
import pathlib

from tests._walk_primitives import (
    PACKAGE_ROOTS,
    REPO_ROOT,
    SCAN_ROOTS,
    package_roots,
    python_bearing_roots,
    scan_roots,
    uv_workspace_members,
)
from tests._walk_primitives import test_roots as derived_test_roots


def test_scan_roots_equal_every_python_bearing_tree() -> None:
    """SCAN_ROOTS is the derived tuple of every python-bearing top-level dir.

    This equality pin IS the scope law's declaration, so the completeness audit
    (test_scan_scope_completeness) exempts this one file by name.
    """
    assert SCAN_ROOTS == (
        "packages/core",
        "packages/mcp",
        "packages/rest",
        "packages/sdk",
        "scripts",
        "tests",
    )


def test_constant_matches_a_fresh_derivation() -> None:
    """The module constant and a re-derivation agree (drift would mean import-order state)."""
    assert SCAN_ROOTS == scan_roots()


def test_hidden_and_symlinked_dirs_never_enter_the_surface(tmp_path: pathlib.Path) -> None:
    """A hidden cache tree and a symlinked venv are not shipped code, never scanned."""
    (tmp_path / ".cache").mkdir()
    (tmp_path / ".cache" / "stowaway.py").write_text("x = 1\n")
    (tmp_path / "real").mkdir()
    (tmp_path / "real" / "mod.py").write_text("x = 1\n")
    (tmp_path / ".venv").symlink_to(tmp_path / "real")
    assert python_bearing_roots(tmp_path) == ("real",)


def test_an_empty_surface_fails_loud(tmp_path: pathlib.Path) -> None:
    """A derivation finding no python root is a broken repo, never a vacuous green."""
    import pytest

    with pytest.raises(AssertionError, match="no python-bearing roots"):
        python_bearing_roots(tmp_path)


def test_package_roots_match_a_fresh_derivation() -> None:
    """The PACKAGE_ROOTS constant and a re-derivation agree (import-order drift, not layout)."""
    assert PACKAGE_ROOTS == package_roots()


def test_scan_roots_never_names_the_member_container(tmp_path: pathlib.Path) -> None:
    """The Wave A shape: ``packages/`` (a dir holding only member packages) is never
    itself a scan root -- the member root ``packages/core`` already covers its tree,
    so naming both would double-scan it."""
    for member in (
        "packages/core",
        "packages/core/crawlutron",
        "packages/mcp",
        "packages/mcp/crawlutron_mcp",
        "packages/rest",
        "packages/rest/crawlutron_rest",
        "packages/sdk",
        "packages/sdk/crawlutron_sdk",
    ):
        (tmp_path / member).mkdir(parents=True)
    (tmp_path / "packages/core/crawlutron/mod.py").write_text("x = 1\n")
    (tmp_path / "packages/mcp/crawlutron_mcp/mod.py").write_text("x = 1\n")
    (tmp_path / "packages/rest/crawlutron_rest/mod.py").write_text("x = 1\n")
    (tmp_path / "packages/sdk/crawlutron_sdk/mod.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n',
        encoding="utf-8",
    )
    assert PACKAGE_ROOTS == package_roots(tmp_path)
    assert "packages" not in scan_roots(tmp_path)
    assert set(scan_roots(tmp_path)) == set(package_roots(tmp_path))


def _write_workspace(tmp_path: pathlib.Path, members: tuple[str, ...], py_members: tuple[str, ...]) -> None:
    """Create each member dir; python-bearing members hold a module."""
    for member in members:
        (tmp_path / member).mkdir(parents=True)
    for py in py_members:
        (tmp_path / py / "mod.py").write_text("x = 1\n")
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n',
        encoding="utf-8",
    )


def test_uv_workspace_member_glob_expands_to_python_bearing_members(tmp_path: pathlib.Path) -> None:
    """A ``packages/*`` member glob resolves to the concrete python-bearing members;
    a stubbed member (docs-only) is not a package root."""
    _write_workspace(
        tmp_path, ("packages/core", "packages/sdk", "packages/docs"), ("packages/core", "packages/sdk")
    )
    assert package_roots(tmp_path) == ("packages/core", "packages/sdk")


def test_uv_workspace_members_drive_package_roots_when_present(tmp_path: pathlib.Path) -> None:
    """A UV workspace declaration is the key: its python-bearing members become the
    package roots (the Wave A seam), and a stubbed member is not a package root."""
    _write_workspace(
        tmp_path, ("packages/core", "packages/sdk", "packages/docs"), ("packages/core", "packages/sdk")
    )
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/core", "packages/sdk", "packages/docs"]\n',
        encoding="utf-8",
    )
    assert uv_workspace_members(tmp_path) == (
        "packages/core",
        "packages/sdk",
        "packages/docs",
    )
    assert package_roots(tmp_path) == ("packages/core", "packages/sdk")


def test_a_workspace_of_only_stub_members_fails_loud(tmp_path: pathlib.Path) -> None:
    """The Wave A seam is not vacuous: a workspace whose members hold no python is a
    broken packaging surface, never an empty green."""
    import pytest

    (tmp_path / "packages" / "docs").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/docs"]\n',
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="no python-bearing dirs"):
        package_roots(tmp_path)


def test_uv_workspace_is_declared() -> None:
    """The uv member manifest is present and expands to a python-bearing member."""
    members = uv_workspace_members()
    assert members is not None, "pyproject.toml declares no [tool.uv.workspace].members"
    assert package_roots(), "pyproject.toml's uv members expand to no python-bearing member"


_PYTEST_INI = REPO_ROOT / "pytest.ini"
_PYPROJECT = REPO_ROOT / "pyproject.toml"


def test_pytest_testpaths_equal_the_derived_test_roots() -> None:
    """pytest.ini testpaths is the derived test surface (#908): a per-package
    home must be a named testpath or the battery stops collecting it (the RED
    this cohort recorded), and a stray testpath naming nothing derived must red."""
    parser = configparser.ConfigParser()
    parser.read(_PYTEST_INI)
    declared = set(parser["pytest"]["testpaths"].split())
    for root in derived_test_roots():
        assert root in declared, f"pytest.ini testpaths is missing the derived test root {root!r}"
    assert declared <= set(derived_test_roots()), (
        "pytest.ini testpaths names non-derived test roots: "
        + repr(sorted(declared - set(derived_test_roots())))
    )


def test_a_ruff_per_file_ignore_covers_every_derived_test_home() -> None:
    """Every derived test home has a ruff per-file-ignore glob, so a moved test
    keeps its flat test-tree lint exemptions (#908); a future ``packages/
    <member>/tests`` without one reds here naming it."""
    import fnmatch
    import tomllib

    with _PYPROJECT.open("rb") as handle:
        table = tomllib.load(handle)
    ruff = table["tool"]["ruff"]["lint"]["per-file-ignores"]
    ignore_globs = list(ruff)
    for root in derived_test_roots():
        probe = f"{root}/probe.py"
        covered = any(fnmatch.fnmatchcase(probe, glob) for glob in ignore_globs)
        assert covered, f"no ruff per-file-ignore glob covers the derived test home {root!r}"
