"""Every Python file holds at most 500 lines.

A file past the ceiling splits along a seam. The budget below is the ratchet:
exact per root, only moves down, ends at zero.
"""

from __future__ import annotations

import pathlib

from tests._walk_primitives import REPO_ROOT, declared_root_files

#: Total lines one file may hold.
LIMIT = 500

#: Files over the ceiling per root. Exact both ways; a split wave lowers it here.
#: Keyed by declared scan root -- a missing root fails loud, not silently empty.
_BUDGETS = {
    "crawlutron": 0,
    "crawlutron_sdk": 0,
    "tests": 0,
    "scripts": 0,
}


def scan(base: pathlib.Path, *, include_gate: bool = False) -> dict[str, int]:
    """Every ``*.py`` over ``LIMIT`` under ``base``, as ``{file: lines}``.

    ``include_gate`` also reads the extensionless ``scripts/gate`` helper, which
    every hook routes through and every ``*.py`` glob misses."""
    paths = sorted(p for p in base.rglob("*.py") if "__pycache__" not in p.parts)
    if include_gate and base.name == "scripts":
        gate = base / "gate"
        if gate.exists():
            paths.append(gate)
    return _over_line_counts(paths)


def overlong(root: str) -> dict[str, int]:
    """Every overlong file under one declared repo root, as ``{file: lines}``.

    ``scripts`` is the one root with an extensionless helper every hook routes
    through, so it is swept with the gate included. The root walk goes through
    the fail-loud helper: a missing root is a gate defect, never an empty scan.
    """
    candidates = list(declared_root_files(root))
    if root == "scripts":
        gate = candidates[0].parents[0] / "gate"
        if gate.exists():
            candidates.append(gate)
    return _over_line_counts(candidates)


def _over_line_counts(paths: list[pathlib.Path]) -> dict[str, int]:
    """The shared body both sweeps measure with: ``{file: lines}`` over LIMIT."""
    found: dict[str, int] = {}
    for path in paths:
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > LIMIT:
            found[path.as_posix()] = lines
    return found


def test_scan_includes_the_extensionless_gate_helper(tmp_path: pathlib.Path) -> None:
    """The H1 reach: the extensionless scripts/gate is not invisible to the file gate.

    Every hook routes through scripts/gate and a ``*.py`` glob misses it; a gate
    that cannot see the helper it is supposed to bound is no gate."""
    scripts = tmp_path / "scripts"
    scripts.mkdir(parents=True)
    gate = scripts / "gate"
    gate.write_text("#!/bin/sh\n" + "\n".join(f"# step {i}" for i in range(501)) + "\n")
    (scripts / "gate.py").write_text("x = 1\n")
    found = scan(scripts, include_gate=True)
    assert list(found) == [gate.as_posix()], f"the gate helper was not scanned: {found}"
    assert found[gate.as_posix()] == 502


def _python_bearing_roots() -> set[str]:
    """Top-level dirs holding any ``*.py``, derived by walking REPO_ROOT.

    Hidden dirs are skipped: they hold environment/cache trees, not shipped
    code, and a dot-named root could never be a declared scan root."""
    return {
        d.name
        for d in REPO_ROOT.iterdir()
        if d.is_dir()
        and not d.name.startswith(".")
        and any("__pycache__" not in q.parts for q in d.rglob("*.py"))
    }


def test_every_declared_scan_root_has_a_budget_row() -> None:
    """The derived side seals: no undeclared python root escapes the ceiling.
    The table side seals: no dead budget row points at a vanished root."""
    assert set(_BUDGETS) == _python_bearing_roots()


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
