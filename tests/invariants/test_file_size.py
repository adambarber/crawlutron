"""Every Python file holds at most 500 lines.

A file past the ceiling splits along a seam. The budget below is the ratchet:
exact per root, only moves down, ends at zero.
"""

from __future__ import annotations

import pathlib

from tests.chokepoint_walks import REPO_ROOT

#: Total lines one file may hold.
LIMIT = 500

#: Files over the ceiling per root. Exact both ways; a split wave lowers it here.
_BUDGETS = {
    "app": 10,
    "tests": 13,
    "scripts": 1,
}


def scan(base: pathlib.Path) -> dict[str, int]:
    """Every ``*.py`` over ``LIMIT`` under ``base``, as ``{file: lines}``."""
    found: dict[str, int] = {}
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > LIMIT:
            found[path.as_posix()] = lines
    return found


def overlong(root: str) -> dict[str, int]:
    """Every overlong file under one repo root, as ``{file: lines}``."""
    return scan(REPO_ROOT / root)


def test_the_detector_bites_over_the_ceiling(
    tmp_path: pathlib.Path,
) -> None:
    """A planted 501-line file fires; a 500-line file stays clean."""
    over = tmp_path / "over.py"
    over.write_text("\n".join(f"x_{i} = {i}" for i in range(501)) + "\n")
    ok = tmp_path / "ok.py"
    ok.write_text("\n".join(f"x_{i} = {i}" for i in range(500)) + "\n")
    found = scan(tmp_path)
    assert list(found) == [over.as_posix()]
    assert found[over.as_posix()] == 501


def test_every_root_is_at_its_declared_file_budget() -> None:
    """No new overlong file lands, and no split is left unclaimed."""
    failures: list[str] = []
    for root, budget in _BUDGETS.items():
        found = overlong(root)
        if len(found) != budget:
            worst = sorted(found.items(), key=lambda kv: -kv[1])[:5]
            detail = "; ".join(f"{name}: {lines}" for name, lines in worst)
            failures.append(f"{root}: {len(found)} overlong, budget {budget} ({detail}).")
    assert not failures, "\n".join(failures)
