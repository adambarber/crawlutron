"""The retired pressure_* admission machinery leaves zero references (#889 C4).

WeightedAdmission, wait_while_saturated and the rest die with the kernel
adoption; any string naming them in code is a void patch. Docs are not scanned."""

from __future__ import annotations

import ast
import pathlib

from tests._walk_primitives import REPO_ROOT, SCAN_ROOTS, comment_lines, is_exempt, string_constants

__all__ = ["RETIRED_SYMBOLS"]

# The retired symbols, exact (deletion surfaces named on the ticket).
RETIRED_SYMBOLS = (
    "WeightedAdmission",
    "wait_while_saturated",
    "saturated_dispatch_floor",
    "pinned_resource_gate",
)

# Ratcheted down-only. One forgiving edge: admission/policy.py's compat
# docstring retells the legacy name to explain the floor_fraction retype.
EXEMPTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "packages/core/src/admission/policy.py",
        "saturated_dispatch_floor",
        "kernel PacingPolicy compat docstring retelling the legacy name (historical record)",
    ),
    (
        "tests/invariants/test_no_weighted_admission_survivors.py",
        "",
        "this module's own ledger table, probes, and docstrings",
    ),
    (
        "tests/admission/test_admission_doubles.py",
        "pinned_resource_gate",
        "the row-9 inventory's absence-probes for the retired factory (its purpose)",
    ),
)


def _exempt(relative: str, value: str) -> bool:
    return is_exempt(relative, value, EXEMPTIONS)


def _offenders(base: pathlib.Path | None = None) -> list[str]:
    offenders: list[str] = []
    roots = (REPO_ROOT / r for r in SCAN_ROOTS) if base is None else (base,)
    for base_dir in roots:
        for path in sorted(base_dir.rglob("*.py")):
            relative = path.relative_to(REPO_ROOT).as_posix() if base is None else path.as_posix()
            source = path.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for lineno, value in string_constants(tree):
                for symbol in RETIRED_SYMBOLS:
                    if symbol in value and not _exempt(relative, value):
                        offenders.append(f"{relative}:{lineno} names {symbol}: {value[:80]!r}")
            for lineno, comment in comment_lines(source):
                for symbol in RETIRED_SYMBOLS:
                    if symbol in comment and not _exempt(relative, comment):
                        offenders.append(f"{relative}:{lineno} COMMENT names {symbol}: {comment[:80]!r}")
    return offenders


def test_no_retired_admission_symbol_survivors() -> None:
    offenders = _offenders()
    assert not offenders, (
        "retired admission machinery referenced in code "
        "(repoint to the kernel gate; a string naming the dead knob is a void patch): " + "; ".join(offenders)
    )


def test_retired_symbol_detector_bites(tmp_path: pathlib.Path) -> None:
    """Mutation check: a stale string to a retired symbol is named file:line."""
    probe = tmp_path / "probe.py"
    probe.write_text(
        "x = monkeypatch.setattr('crawlutron.runtime.pressure_sizing.WeightedAdmission', fake)\n",
        encoding="utf-8",
    )
    hits = _offenders(tmp_path)
    assert any("names WeightedAdmission" in h and "probe.py:1" in h for h in hits), hits
