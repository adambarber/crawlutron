"""Park after a release must genuinely suspend the event loop."""

from __future__ import annotations

import asyncio

from tests._support._doors import _ceiling_one_gate, _live


async def test_park_after_release_yields_the_event_loop() -> None:
    """A parked admit after a release suspends; a cooperating task progresses."""
    gate, want = _ceiling_one_gate()  # GENERAL ceiling 1

    # Parker C starts before A enters admit, so C's context-var copy sees no
    # live lease (admit contexts are task-isolated).
    start_park = asyncio.Event()

    async def _parker():
        await start_park.wait()
        return await _live(gate, want)

    park_c = asyncio.create_task(_parker())

    # Holder A occupies the sole slot (in this task's context only).
    cm_a, granted_a = await _live(gate, want)

    start_park.set()
    await asyncio.sleep(0)  # let C reach the suspended park wait

    # Release A: set() wakes C; C re-decides with a free slot and grants.
    granted_a.lease.release(work_completed=True)
    await cm_a.__aexit__(None, None, None)
    cm_c, granted_c = await park_c

    # Parker D parks while C holds the slot. An edge-triggered signal makes D's
    # wait() suspend (latch consumed by C's wake); a level-latched signal spins.
    progressed = asyncio.Event()

    async def _cooperator() -> None:
        await asyncio.sleep(0)
        progressed.set()

    asyncio.create_task(_cooperator())
    park_d = asyncio.create_task(_parker())
    try:
        await asyncio.wait_for(progressed.wait(), timeout=5.0)
    except TimeoutError as exc:
        raise AssertionError(
            "park-after-release did not suspend the event loop: a level-latched "
            "Signal returned from wait() on an already-set event and spun "
            "synchronously, starving the cooperating task"
        ) from exc

    # Release C: edge-triggered set() wakes D exactly once; D grants.
    granted_c.lease.release(work_completed=True)
    await cm_c.__aexit__(None, None, None)
    cm_d, granted_d = await park_d
    assert granted_d.allocation.total.value == 1
    await cm_d.__aexit__(None, None, None)
