"""The scan surface is derived by walking the repo, never hand-listed."""

from __future__ import annotations

import pathlib

from tests._walk_primitives import SCAN_ROOTS, python_bearing_roots


def test_scan_roots_equal_every_python_bearing_tree() -> None:
    """SCAN_ROOTS is the derived tuple of every python-bearing top-level dir.

    This equality pin IS the scope law's declaration, so the completeness audit
    (test_scan_scope_completeness) exempts this one file by name.
    """
    assert SCAN_ROOTS == ("crawlutron", "crawlutron_sdk", "scripts", "tests")


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
