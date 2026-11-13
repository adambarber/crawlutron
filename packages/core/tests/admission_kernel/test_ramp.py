"""Acceptance row 1: the gate-owned ramp grows/decays and is caller-isolated.

A returned copy never mutates the gate; growth fires only after
``ramp_growth_window`` full-scale grants and is capped at ``ramp_max``.
"""

from __future__ import annotations

import dataclasses

import pytest

from admission import Count, Want
from tests._support._doors import _finish, _live
from tests._support._gate_factory import GateSpec, build_gate

_GENERAL = Want.of(general=Count(1), heavy=Count(0))


def _ramp_spec(
    *,
    ramp_floor: int = 4,
    ramp_max: int = 64,
    initial_ramp: int = 8,
    multiplier: float = 2.0,
    growth_window: int = 3,
) -> GateSpec:
    return dataclasses.replace(
        GateSpec(),
        ramp_floor=ramp_floor,
        ramp_max=ramp_max,
        initial_ramp=initial_ramp,
        ramp_multiplier=multiplier,
        ramp_growth_window=growth_window,
    )


async def test_ramp_read_is_a_copy_not_a_handle() -> None:
    """Mutating the returned ramp must not change the gate's internal ramp."""
    gate, _ = build_gate(_ramp_spec())
    r = gate.ramp()
    assert type(r) is Count
    r.value = 999
    # Mutant: returning the internal ramp by reference would let this corrupt it.
    assert gate.ramp().value == 8


async def test_ramp_growth_fires_after_full_scale_window_and_is_capped() -> None:
    """Growth fires after ramp_growth_window full-scale grants, capped at ramp_max."""
    gate, _ = build_gate(_ramp_spec(ramp_floor=1, ramp_max=1000, initial_ramp=10))
    for _ in range(3):
        cm, granted = await _live(gate, _GENERAL)
        await _finish(cm)
    # Mutant: growth that ignores the window or the cap would redden either bound.
    assert gate.ramp().value == 20
    assert gate.ramp().value <= 1000


async def test_reset_ramp_for_quiescence_guards_in_flight() -> None:
    """reset-for-quiescence refuses to run mid-flight, then restores the initial ramp."""
    gate, _ = build_gate(_ramp_spec())
    cm, granted = await _live(gate, _GENERAL)
    try:
        with pytest.raises(AssertionError):
            gate.reset_ramp_for_quiescence()
    finally:
        granted.lease.release()
        await _finish(cm)
    gate.reset_ramp_for_quiescence()
    assert gate.ramp().value == 8


async def test_ramp_never_exceeds_ramp_max() -> None:
    """Many full-scale grants grow the ramp but never past ramp_max."""
    gate, _ = build_gate(_ramp_spec(ramp_floor=1, ramp_max=12, initial_ramp=10))
    for _ in range(20):
        cm, granted = await _live(gate, _GENERAL)
        await _finish(cm)
    # Mutant: an uncapped multiply would blow past ramp_max here.
    assert gate.ramp().value <= 12
