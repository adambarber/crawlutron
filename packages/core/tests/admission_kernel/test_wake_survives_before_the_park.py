"""A wake must survive arriving before the waiter parks.

The park loop re-decides only after waking and PARK has no deadline, so a `set` with
no parked waiter is lost; the doubles latch, so only the production seam shows it.
"""

from __future__ import annotations

import asyncio

from admission import Count, LaneId, Want
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import (
    TaskRamBudget,
    build_admission_gate,
    build_admission_policy,
)
from tests._support._doors import _live


def _gate_with_a_full_general_lane():
    """A production-seam gate plus the want that fills its GENERAL lane entirely."""
    policy = build_admission_policy(
        3 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=1, budget=TaskRamBudget()
    )
    gate = ResourceGate(memory_ratio=lambda: 0.10, cpu_ratio=lambda: 0.10)
    gate.memory.target, gate.memory.hard = 0.85, 0.90
    gate.cpu.target, gate.cpu.hard = 0.90, 0.95
    admission = build_admission_gate(gate, policy, TaskRamBudget())
    ceiling = admission._lanes[LaneId.GENERAL].ceiling.value
    return admission, Want.of(general=Count(ceiling), heavy=Count(0))


async def test_a_wake_delivered_before_the_park_is_not_lost() -> None:
    """The red: the only release lands before the parker's future is registered."""
    gate, filler = _gate_with_a_full_general_lane()
    one = Want.of(general=Count(1), heavy=Count(0))
    start_park = asyncio.Event()

    async def _parker():
        await start_park.wait()
        return await _live(gate, one)

    # Created before the holder takes the lease, so its context copy sees none.
    parker = asyncio.create_task(_parker())

    cm_a, granted_a = await _live(gate, filler)
    real_wait = gate._bounded_wait
    fired = {"done": False}

    async def racing_wait(signal, timeout):
        """Release the lane exactly once, before the parker has a waiter to wake."""
        if not fired["done"]:
            fired["done"] = True
            granted_a.lease.release(work_completed=True)  # frees the lane AND sets
        await real_wait(signal, timeout)

    gate._bounded_wait = racing_wait
    try:
        start_park.set()
        await asyncio.sleep(0)  # let the parker decide PARK and hit the seam
        assert fired["done"], "the parker never parked, so this pins nothing"
        # Pre-fix this hangs: the set() found no waiter and the signal forgot it, so
        # a free lane still never grants (PARK has no deadline).
        cm_b, granted_b = await asyncio.wait_for(parker, timeout=5.0)
    finally:
        gate._bounded_wait = real_wait
        await cm_a.__aexit__(None, None, None)
    assert granted_b.allocation.total.value == 1
    await cm_b.__aexit__(None, None, None)
