"""The gate lock is FREE across the internal park, proven across two OS threads.

A parked admit (RETRY_PRESSURE in ``__aenter__``) must not hold the threading.Lock;
a second OS thread acquires it (a release) and wakes the park to a grant. Bounded.
"""

from __future__ import annotations

import asyncio
import dataclasses
import queue
import threading

from tests.admission_kernel._doors import Count, Granted, LaneId, Want
from tests.admission_kernel._gate_factory import GateSpec, build_gate

_WANT = Want.of(general=Count(1), heavy=Count(0))


def test_parked_admit_wakes_while_other_thread_releases() -> None:
    """A third OS thread releases the holder while a parker waits; the park wakes with a grant."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(general_ceiling=1), thread_safe_signal=True))
    first_q: queue.Queue[Granted] = queue.Queue(maxsize=1)
    held = threading.Event()
    parker_started = threading.Event()
    stop = threading.Event()
    granted: list[int] = []

    async def _hold() -> None:
        # Thread A: hold the ceiling-full grant live so the parker has nothing to fit.
        cm1 = gate.admit(_WANT)
        first = await cm1.__aenter__()
        first_q.put(first)
        held.set()
        await asyncio.to_thread(stop.wait)  # keep the loop alive holding the lease
        await cm1.__aexit__(None, None, None)

    async def _park() -> None:
        # Thread B (its own context): a concurrent admit on the full ceiling parks.
        cm2 = gate.admit(_WANT)
        g2 = await cm2.__aenter__()  # wakes when the main thread releases the holder
        granted.append(g2.allocation.for_lane(LaneId.GENERAL).value)
        g2.lease.release(work_completed=True)
        await cm2.__aexit__(None, None, None)

    def _parker() -> None:
        parker_started.set()
        asyncio.run(_park())

    holder = threading.Thread(target=lambda: asyncio.run(_hold()), name="holder")
    parker = threading.Thread(target=_parker, name="parker")
    holder.start()
    first = first_q.get(timeout=2.0)
    held.wait(timeout=2.0)
    parker.start()
    parker_started.wait(timeout=2.0)
    threading.Event().wait(0.05)  # let the parker reach the __aenter__ park
    # Mutant: if the park held the gate lock, this release (a different OS thread)
    # would deadlock; a free-lock park lets it through and wakes the parker.
    first.lease.release(work_completed=True)
    stop.set()
    parker.join(timeout=5.0)
    holder.join(timeout=5.0)
    assert not parker.is_alive() and not holder.is_alive(), "a cross-thread park deadlocked"
    # Mutant: a release that did not wake the park would leave the grant unmade.
    assert granted == [1]
