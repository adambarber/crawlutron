"""The cold-probe counter counts one per DECISION, never per lane.

A cold grant spanning two lanes (one cold sensor) debits one cold-probe slot and
returns it to zero on release; a residual counter would park every cold admit.
"""

from __future__ import annotations

import asyncio
import dataclasses

from tests.admission_kernel._doors import Count, LaneId, Want
from tests.admission_kernel._gate_factory import FakeGauge, GateSpec, build_gate

_DUAL = Want.of(general=Count(1), heavy=Count(1))


def _cold_dual_gate():
    """A gate whose CPU is cold (probe 1) and whose budget fits a dual-lane grant."""
    cpu_cold = FakeGauge(0.5, warm=False)
    gate, _ = build_gate(
        dataclasses.replace(
            GateSpec(cold_probe=1),
            cpu_gauge=cpu_cold,
            memory_budget_mib=700,
        )
    )
    return gate


async def _acquire_release(gate, want):
    """Acquire and release one cold dual-lane grant, returning its allocation."""
    cm = gate.admit(want)
    granted = await cm.__aenter__()
    allocation = granted.allocation
    granted.lease.release(work_completed=True)
    await cm.__aexit__(None, None, None)
    return allocation


async def test_cold_probe_counts_once_per_decision_across_lanes() -> None:
    """A dual-lane cold grant consumes one cold-probe slot; release returns it to zero."""
    gate = _cold_dual_gate()
    allocation = await _acquire_release(gate, _DUAL)
    assert allocation.for_lane(LaneId.GENERAL).value == 1
    assert allocation.for_lane(LaneId.HEAVY).value == 1
    # Mutant: debiting per lane (two lanes, one decision) leaks a counter slot.
    assert gate.cold_in_flight().value == 0


async def test_follow_up_cold_admit_after_release_is_not_parked() -> None:
    """After a dual-lane cold grant is released, the next cold admit grants, not parks."""
    gate = _cold_dual_gate()
    await _acquire_release(gate, _DUAL)

    async def readmit() -> int:
        cm2 = gate.admit(_DUAL)
        g2 = await cm2.__aenter__()
        got = g2.allocation.total.value
        g2.lease.release(work_completed=True)
        await cm2.__aexit__(None, None, None)
        return got

    # A follow-up want must not park on a leaked cold-probe exhaustion.
    granted_total = await asyncio.wait_for(readmit(), timeout=1.0)
    # Mutant: a leaked cold-probe slot would park this follow-up forever (the
    # would-be OVERSIZE hangs in RETRY_PRESSURE, timing out above); a live box grants.
    assert granted_total >= 1
