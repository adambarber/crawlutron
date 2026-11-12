"""The scan surface is derived by walking the repo, never hand-listed."""

from __future__ import annotations

import pathlib

from tests._walk_primitives import (
    PACKAGE_ROOTS,
    SCAN_ROOTS,
    package_roots,
    python_bearing_roots,
    uv_workspace_members,
)


def test_scan_roots_equal_every_python_bearing_tree() -> None:
    """SCAN_ROOTS is the derived tuple of every python-bearing top-level dir.

    This equality pin IS the scope law's declaration, so the completeness audit
    (test_scan_scope_completeness) exempts this one file by name.
    """
    assert SCAN_ROOTS == ("admission", "crawlutron", "crawlutron_sdk", "scripts", "tests")


def test_constant_matches_a_fresh_derivation() -> None:
    """The module constant and a re-derivation agree (drift would mean import-order state)."""
    assert SCAN_ROOTS == python_bearing_roots()


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


def test_package_roots_equal_the_scan_surface_in_the_current_layout() -> None:
    """PACKAGE_ROOTS is SCAN_ROOTS today: the aggregate top-level layout predates UV
    members, so every scan root IS a package root -- the growth direction fails loud
    when a tree is added without SCAN_ROOTS, and staled-fails when either drifts."""
    assert PACKAGE_ROOTS == SCAN_ROOTS == python_bearing_roots()


def test_package_roots_match_a_fresh_derivation() -> None:
    """The PACKAGE_ROOTS constant and a re-derivation agree (import-order drift, not layout)."""
    assert PACKAGE_ROOTS == package_roots()


def test_uv_workspace_members_drive_package_roots_when_present(tmp_path: pathlib.Path) -> None:
    """A UV workspace declaration is the key: its python-bearing members become the
    package roots (the Wave A seam), and a stubbed member is not a package root."""
    for member in ("packages/core", "packages/sdk", "packages/docs"):
        (tmp_path / member).mkdir(parents=True)
    (tmp_path / "packages/core" / "mod.py").write_text("x = 1\n")
    (tmp_path / "packages/sdk" / "mod.py").write_text("x = 1\n")
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
