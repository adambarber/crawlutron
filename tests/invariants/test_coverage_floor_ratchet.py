"""Per-package coverage floors are derived from one measured artifact and ratchet up (#1358)."""

from __future__ import annotations

import math

from scripts.coverage_floor_gate import check_floors
from tests._walk_primitives import package_roots
from tests.invariants._coverage_floors import MEASURED_PACKAGES, OVERALL_FLOOR, PACKAGE_FLOORS


def test_floor_gate_names_the_package_and_floor_on_a_drop() -> None:
    """A measured value below its seed fails, naming both; neutralize the check and this reds."""
    measured = dict.fromkeys(PACKAGE_FLOORS, 100.0)
    measured["packages/extract"] = 90.0
    violations = check_floors(measured, PACKAGE_FLOORS)
    assert len(violations) == 1
    assert "packages/extract" in violations[0]
    assert "92.5" in violations[0]


def test_floor_gate_passes_when_every_package_meets_its_floor() -> None:
    """All-meets-floor is green, so the gate is not a constant refusal."""
    assert check_floors(dict.fromkeys(PACKAGE_FLOORS, 100.0), PACKAGE_FLOORS) == []


def test_floors_cover_every_derived_package_root() -> None:
    """A new uv member cannot skip the floor: roots are derived, never hand-listed here."""
    assert set(PACKAGE_FLOORS) == set(package_roots())


def test_overall_floor_is_the_owner_ruled_value() -> None:
    """The hook's --cov-fail-under literal is pinned to this value by the identity test."""
    assert OVERALL_FLOOR == 90.0


def test_floors_are_the_measured_seeds_not_hand_typed() -> None:
    """A floor lowered alone reds; a raise must update the measured seed in the same commit."""
    assert PACKAGE_FLOORS == {root: math.floor(value * 10) / 10 for root, value in MEASURED_PACKAGES.items()}
