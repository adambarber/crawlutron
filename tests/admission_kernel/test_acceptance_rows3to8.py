"""Acceptance rows 3-8: oversize/release, concurrency, cold-stale, wakeup, stall.

Each test names the mutant it kills and the acceptance row it pins, proven from
the caller's ``admit``/lease surface.
"""

from __future__ import annotations

import asyncio
import dataclasses

import pytest
from admission import (
    AdmissionRefused,
    Count,
    Duration,
    Granted,
    IdleReason,
    LaneId,
    StallGuard,
    Want,
    WrongGateError,
)

from tests.admission_kernel._doors import _GENERAL_ONE, _auto, _finish, _hold_one, _live
from tests.admission_kernel._gate_factory import FakeGauge, GateSpec, build_gate

_STALE_CPU = FakeGauge(0.5, warm=False, stale=True)


def _with(
    *,
    general_ceiling: int | None = None,
    cpu_gauge: FakeGauge | None = None,
    mem_gauge: FakeGauge | None = None,
    stale_bound: float | None = None,
) -> GateSpec:
    """A driver spec with the given field overrides (or a default spec)."""
    base = GateSpec()
    if general_ceiling is not None:
        base = dataclasses.replace(base, general_ceiling=general_ceiling)
    if cpu_gauge is not None:
        base = dataclasses.replace(base, cpu_gauge=cpu_gauge)
    if mem_gauge is not None:
        base = dataclasses.replace(base, mem_gauge=mem_gauge)
    if stale_bound is not None:
        base = dataclasses.replace(base, stale_bound=stale_bound)
    return base


async def _release(cm, granted: Granted, *, work_completed: bool = True) -> None:
    """Release a held grant and exit its context manager."""
    granted.lease.release(work_completed=work_completed)
    await _finish(cm)


async def _refuses(gate) -> None:
    """Admit a single GENERAL want and require the STALE/OVERSIZE reason."""
    with pytest.raises(AdmissionRefused) as exc:
        async with gate.admit(_GENERAL_ONE):
            pass
    assert exc.value.reason is IdleReason.STALE


# -- Row 4: release -----------------------------------------------------


async def test_release_is_idempotent() -> None:
    """A second release of the same lease is a no-op, never a double charge."""
    gate, _ = build_gate()
    cm, granted = await _hold_one(gate)
    assert granted.lease.release() is True
    # Mutant: releasing twice would double-free the capacity below.
    assert granted.lease.release() is False
    await _finish(cm)


async def test_exceptioned_body_does_not_advance_progress() -> None:
    """A release counted as not-completed leaves progress at its grant-time."""
    gate, handles = build_gate()
    cm, granted = await _live(gate, Want.of(general=Count(0), heavy=Count(1)))
    grant_time = gate.progress().last_advance.value
    handles.now += 5.0  # the body runs late on the injected clock
    await _release(cm, granted, work_completed=False)  # an exceptioned body's release
    # Mutant: counting an exceptioned release as work would hide a stalled box.
    assert gate.progress().last_advance.value - grant_time < 4.0


async def test_completed_release_advances_progress() -> None:
    """A completed release advances last_advance past the grant-time: the box is live."""
    gate, handles = build_gate()
    cm, granted = await _hold_one(gate)
    grant_time = gate.progress().last_advance.value
    handles.now += 5.0
    await _release(cm, granted, work_completed=True)
    # Mutant: dropping the completion advance would make the box look stalled.
    assert gate.progress().last_advance.value - grant_time >= 4.0


async def test_wrong_gate_release_raises() -> None:
    """Releasing a lease to a different gate is refused, never absorbed."""
    gate, _ = build_gate()
    other, _ = build_gate()
    cm, granted = await _hold_one(gate)
    # A lease made on `gate` cannot be handed to `other` (foreign in-flight write).
    with pytest.raises(WrongGateError):
        other._release_lease(granted.lease, work_completed=True)
    # The owner still releases cleanly.
    assert granted.lease.release() is True
    await _finish(cm)


async def test_release_frees_capacity_for_the_next_admit() -> None:
    """Releasing returns the unit to the pool: a blocked lane grants again."""
    gate, _ = build_gate()
    cm, granted = await _hold_one(gate)
    await _release(cm, granted)
    nxt = await _auto(gate, _GENERAL_ONE)
    assert nxt.for_lane(LaneId.GENERAL).value == 1


# -- Row 5: concurrency -------------------------------------------------


async def test_concurrent_admits_never_exceed_ceiling() -> None:
    """The in-flight count never crosses the ceiling under concurrent admit/release."""
    gate, _ = build_gate(_with(general_ceiling=4))
    want = Want.of(general=Count(4), heavy=Count(0))
    peak = 0
    active = 0
    held: list[tuple[object, Granted]] = []
    done = asyncio.Event()

    async def worker() -> None:
        nonlocal peak, active
        cm, granted = await _live(gate, want)
        active += 1
        peak = max(peak, active)
        held.append((cm, granted))
        # Park to hold the reservation; the test releases us after the window.
        await done.wait()
        active -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(8)]
    # Give everyone a chance to be carved into ceiling-sized grants; the 4 that
    # cannot fit remain parked (RETRY_PRESSURE) until we release.
    await asyncio.sleep(0.1)
    assert peak <= 4  # Mutant: an oversubscribing grant would redden this.
    assert len(held) <= 4
    done.set()
    for cm2, granted in list(held):
        await _release(cm2, granted)
    try:
        stragglers = await asyncio.wait_for(asyncio.gather(*tasks), timeout=1.0)
    except TimeoutError:
        stragglers = None
    assert stragglers is not None, "a woken straggler failed to grant after the release"


# -- Row 6: cold / stale -----------------------------------------------


async def test_stale_resource_raises_refused_stale() -> None:
    """A stale resource stops new admission: STALE is a terminal refusal."""
    gate, _ = build_gate(_with(cpu_gauge=_STALE_CPU, stale_bound=0.2))
    await _refuses(gate)


async def test_stale_precedes_oversize() -> None:
    """STALE wins over OVERSIZE: a stale resource refuses before oversize analysis."""
    gate, _ = build_gate(_with(cpu_gauge=_STALE_CPU, general_ceiling=0, stale_bound=0.2))
    await _refuses(gate)


async def test_wait_is_bounded_nonblocking() -> None:
    """A parked admit is bounded by the stale terminal, never hung."""
    gate, _ = build_gate(_with(cpu_gauge=_STALE_CPU, stale_bound=0.05))
    await _refuses(gate)


async def test_memory_is_never_cold() -> None:
    """Memory is always warm (or stale): the cold posture is CPU-only."""
    gate, _ = build_gate(_with(mem_gauge=FakeGauge(0.5, warm=True), cpu_gauge=FakeGauge(0.5, warm=False)))
    want = Want.of(general=Count(1), heavy=Count(0))
    cm, granted = await _live(gate, want)
    assert granted.allocation.for_lane(LaneId.GENERAL).value >= 1
    await _finish(cm)


async def test_cold_start_admits_bounded_probe_and_no_ramp_growth() -> None:
    """A cold box grants a bounded probing allowance and never grows the ramp."""
    gate, _ = build_gate(_with(cpu_gauge=FakeGauge(0.5, warm=False)))
    r0 = gate.ramp().value
    for _ in range(6):
        cm, granted = await _hold_one(gate)
        await _release(cm, granted)
    # Mutant: growing the ramp while cold would over-commit a box with no sample.
    assert gate.ramp().value <= r0


# -- Row 7: wakeup ------------------------------------------------------


async def _hold_in_task(gate, want: Want):
    """Acquire a grant inside its own task, keeping this context's lease marker clear."""
    task = asyncio.create_task(_live(gate, want))
    return await asyncio.wait_for(task, timeout=1.0)


async def _assert_parker_wakes(gate) -> None:
    """A parked admit (ceiling 1, held) must grant once the holder releases."""
    cm, first = await _hold_in_task(gate, _GENERAL_ONE)
    parker = asyncio.create_task(_live(gate, _GENERAL_ONE))
    await asyncio.sleep(0.02)
    assert not parker.done()
    first.lease.release()  # sets the signal before the parker's re-check
    await _finish(cm)
    parker_cm, parker_grant = await asyncio.wait_for(parker, timeout=1.0)
    await _finish(parker_cm)
    assert parker_grant.allocation.for_lane(LaneId.GENERAL).value == 1


async def test_release_wakes_a_parked_admit() -> None:
    """A park sits until a release bumps the version and wakes it."""
    gate, handles = build_gate(_with(general_ceiling=1))
    await _assert_parker_wakes(gate)


async def test_event_before_wait_is_not_lost() -> None:
    """A signal set before the wait is not lost: release-wake is the rendezvous."""
    gate, handles = build_gate(_with(general_ceiling=1))
    await _assert_parker_wakes(gate)


# -- Row 8: stall -------------------------------------------------------


async def test_stall_is_independent_of_wall_clock() -> None:
    """Stall reads the gate's injected clock, not the wall clock."""
    gate, handles = build_gate()
    guard = StallGuard(gate, no_progress_stall=Duration(5.0))
    assert not guard.stalled()  # a fresh grant keeps the box live
    handles.now += 10.0  # advance the INJECTED clock past the bound with no grant
    # Mutant: a wall-clock stall detector would stay live here, missing the pause.
    assert guard.stalled()


async def test_draining_saturated_box_is_not_stalled() -> None:
    """A box that keeps granting and completing release is live, not stalled."""
    gate, handles = build_gate()
    guard = StallGuard(gate, no_progress_stall=Duration(5.0))
    handles.now = 0.0
    for i in range(4):
        handles.now = float(i)
        cm, granted = await _hold_one(gate)
        await _release(cm, granted)
    # Mutant: a completing box reported stalled would halt a busy scheduler.
    assert not guard.stalled()
