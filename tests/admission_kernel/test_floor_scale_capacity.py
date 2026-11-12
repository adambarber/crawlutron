"""Floor/scale/capacity laws (5): the floor is a minimum-progress law bounded by want and capacity.

The floor law is a property test under the acceptance regime: a swept floor still grants
>= 1, capacity clamps, and OVERSIZE surfaces only with zero in flight. Each test names its mutant.
"""

from __future__ import annotations

import dataclasses

import pytest

from admission.values import Allocation
from tests.admission_kernel._doors import AdmissionRefused, Count, IdleReason, LaneId, Want
from tests.admission_kernel._gate_factory import GateSpec, build_gate


async def _grant(gate, want: Want) -> Allocation:
    async with gate.admit(want) as granted:
        return granted.allocation


async def test_floor_is_clamped_by_want_and_capacity() -> None:
    """granted_total is in [1, want.total]: a one-item ask grants exactly one, never more."""
    gate, _h = build_gate()
    want = Want.of(general=Count(1), heavy=Count(0))
    allocation = await _grant(gate, want)
    # Mutant: granting more than the want reddens this (a job asking one never gets two).
    assert allocation.for_lane(LaneId.GENERAL).value == 1
    assert allocation.total.value == 1


async def test_granted_total_never_exceeds_want_total_across_domain() -> None:
    """Property over the usage/capacity domain: a grant is in [1, want.total]; a refusal is OVERSIZE."""
    for mem in (0.1, 0.5, 0.7, 0.95):
        for cpu in (0.1, 0.5, 0.95):
            for want_g in (1, 2, 5):
                for want_h in (0, 1):
                    if want_g + want_h < 1:
                        continue
                    gate, _ = build_gate(dataclasses.replace(GateSpec(), memory_usage=mem, cpu_usage=cpu))
                    want = Want.of(general=Count(want_g), heavy=Count(want_h))
                    try:
                        allocation = await _grant(gate, want)
                    except AdmissionRefused as exc:
                        # A refusal is the OVERSIZE terminal, never a silent zero grant.
                        assert exc.reason is IdleReason.OVERSIZE
                        continue
                    total = allocation.total.value
                    # Mutant: granting 0 total, or granting more than the want, reddens this.
                    assert 1 <= total <= want.total.value, (mem, cpu, want_g, want_h, total)


async def test_per_lane_floor_never_zero_when_lane_wanted() -> None:
    """A swept floor_global (1 // fraction == 0 per lane) still grants >= 1 total."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), ramp_floor=1, ramp_max=1, initial_ramp=1))
    want = Want.of(general=Count(1), heavy=Count(1))
    allocation = await _grant(gate, want)
    # Mutant: dropping max(1, floor//spread) would grant 0 here.
    assert allocation.total.value >= 1


async def _assert_oversized(gate, want: Want) -> None:
    """Admit ``want`` must raise AdmissionRefused with the OVERSIZE reason."""
    with pytest.raises(AdmissionRefused) as exc:
        async with gate.admit(want):
            pass
    assert exc.value.reason is IdleReason.OVERSIZE


def _heavy_underfunded():
    """A gate whose HEAVY cost (700 MiB) exceeds the 600 MiB memory budget."""
    return build_gate(dataclasses.replace(GateSpec(), heavy_cost_mib=700, memory_budget_mib=600))


async def test_oversize_want_raises_refused_with_reason_oversize() -> None:
    """A want that can fit no unit with zero in flight raises AdmissionRefused(OVERSIZE)."""
    gate, _ = _heavy_underfunded()
    await _assert_oversized(gate, Want.of(general=Count(0), heavy=Count(1)))


async def test_oversize_evaluates_wanted_lanes_only() -> None:
    """The evaluator reads WANTED lanes only: a fitting GENERAL turns the same memory live."""
    gate, _ = _heavy_underfunded()
    # A larger HEAVY ask is still OVERSIZE alone (wanted-lanes-only evaluates HEAVY).
    await _assert_oversized(gate, Want.of(general=Count(0), heavy=Count(2)))
    # The contrast: a mixed want (GENERAL also wanted) does not oversize.
    mixed = Want.of(general=Count(1), heavy=Count(1))
    allocation = await _grant(gate, mixed)
    assert allocation.total.value >= 1


async def test_zero_ceiling_lane_raises_oversize() -> None:
    """A wanted lane with ceiling 0 and free//cost >= 1 raises OVERSIZE, never a silent zero."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=0))
    await _assert_oversized(gate, Want.of(general=Count(1), heavy=Count(0)))


async def test_heavy_only_want_grants_at_least_one_when_it_fits() -> None:
    """HEAVY grants >= 1 only when free // cost_heavy >= 1 (the heavy minimum)."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), heavy_cost_mib=700, memory_budget_mib=700))
    want = Want.of(general=Count(0), heavy=Count(1))
    allocation = await _grant(gate, want)
    assert allocation.for_lane(LaneId.HEAVY).value >= 1
