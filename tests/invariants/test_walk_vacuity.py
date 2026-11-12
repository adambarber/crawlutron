"""No walk-based gate may scan nothing: each declared root must yield >=1 .py."""

from __future__ import annotations

import pathlib

from tests._walk_primitives import SCAN_ROOTS, empty_scan_roots


def test_every_declared_scan_root_yields_a_python_file() -> None:
    """A walk gate over an empty root is silently vacuous, so every SCAN_ROOTS root must
    resolve to >=1 .py under the current layout, naming any dead root."""
    dead = empty_scan_roots(SCAN_ROOTS)
    assert not dead, f"vacuously-scanned scan roots: {dead}"


def test_an_empty_declared_root_reds_naming_it(tmp_path: pathlib.Path) -> None:
    """Injection: an empty declared root reds naming the root, never a vacuous green."""
    live = tmp_path / "live"
    live.mkdir()
    (live / "mod.py").write_text("x = 1\n")
    empty = tmp_path / "empty"
    empty.mkdir()
    assert empty_scan_roots(("live", "empty"), base=tmp_path) == ("empty",)
