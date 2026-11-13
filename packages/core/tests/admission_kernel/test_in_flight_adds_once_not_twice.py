"""Capacity is ADDITIONAL, not TOTAL: in-flight is subtracted exactly once.

Ceiling/scale terms are TOTAL; memory is already ADDITIONAL. Each held grant is
entered in its own task (a live lease forbids a nested admit)."""

from __future__ import annotations

import asyncio
import dataclasses

from admission import Count, LaneId, Want
from tests._support._doors import _GENERAL_ONE, Granted, _finish, _live
from tests._support._gate_factory import GateSpec, build_gate

_GENERAL_SIX = Want.of(general=Count(6), heavy=Count(0))


async def _hold_one_task(gate, want=_GENERAL_ONE):
    """Enter ``want`` in its own task so a live lease never nests an admit."""
    return await asyncio.wait_for(asyncio.create_task(_live(gate, want)), 1.0)


async def _n_live(gate, n: int) -> list[tuple[object, Granted]]:
    """Hold ``n`` single-GENERAL grants, each in its own task."""
    held: list[tuple[object, Granted]] = []
    for _ in range(n):
        held.append(await _hold_one_task(gate))
    return held


async def _release_all(held: list[tuple[object, Granted]]) -> None:
    for cm, granted in held:
        granted.lease.release(work_completed=True)
        await _finish(cm)


async def _parks(gate, want=_GENERAL_ONE) -> bool:
    """True when an admit of ``want`` parks (does not grant within the bound)."""
    probe = asyncio.create_task(_live(gate, want))
    await asyncio.sleep(0.02)
    parked = not probe.done()
    if parked:
        probe.cancel()
        await asyncio.gather(probe, return_exceptions=True)
    return parked


async def test_second_single_admit_grants_immediately_within_ceiling() -> None:
    """One live GENERAL lease on a ceiling-4 gate does not serialize the second admit."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=4))
    cm1, first = await _hold_one_task(gate)
    cm2, second = await _hold_one_task(gate)  # must GRANT now; the double-subtract parked it
    assert second.allocation.for_lane(LaneId.GENERAL).value == 1
    await _release_all([(cm1, first), (cm2, second)])


async def test_last_free_slot_within_ceiling_is_grantable() -> None:
    """At in-flight == ceiling-1 the final slot is still grantable (nothing to subtract)."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=4))
    held = await _n_live(gate, 3)  # in-flight GENERAL == 3 == ceiling - 1
    cm4, fourth = await _hold_one_task(gate)  # the one remaining slot must grant
    assert fourth.allocation.for_lane(LaneId.GENERAL).value == 1
    await _finish(cm4)
    await _release_all(held)


async def test_admit_at_ceiling_still_parks() -> None:
    """Ceiling enforcement survives: with in-flight == ceiling a further admit parks."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=4))
    held = await _n_live(gate, 4)  # in-flight == ceiling
    assert await _parks(gate), "a fifth admit at ceiling 4 must park, not grant"
    await _release_all(held)


async def test_zero_in_flight_admits_immediately() -> None:
    """The zero boundary: an idle gate grants a single admit with no park."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=4))
    cm, granted = await _hold_one_task(gate)
    assert granted.allocation.for_lane(LaneId.GENERAL).value == 1
    await _finish(cm)


async def test_want_beyond_ceiling_grants_without_oversubscribing() -> None:
    """A want above the ceiling grants a floor, never more than the ceiling."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_ceiling=4))
    cm, granted = await _hold_one_task(gate, _GENERAL_SIX)
    got = granted.allocation.for_lane(LaneId.GENERAL).value
    assert got >= 1
    assert granted.allocation.total.value <= 4  # never more than the ceiling
    await _finish(cm)


async def test_memory_starved_admit_parks_after_in_flight_consumes_budget() -> None:
    """Memory stays ADDITIONAL and authoritative: a want beyond live free memory
    parks (in-flight already consumed the bytes), it does not over-commit."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), general_cost_mib=48, memory_budget_mib=48))
    held = await _n_live(gate, 1)  # consumes the entire 48 MiB budget
    assert await _parks(gate), "a want past live free memory must park, never over-commit"
    await _release_all(held)
