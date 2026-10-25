"""#448 red-first: start_tracked_task's unconditional done-callback pop lets a
stale callback evict a replacement task's registration."""

import asyncio
from collections.abc import Callable


def _finishable_and_blocked_factories() -> tuple[Callable, Callable, asyncio.Event]:
    """A factory that finishes in one turn, and one held open by an event."""
    b_release = asyncio.Event()

    async def fast():
        return "a"

    async def slow():
        await b_release.wait()
        return "b"

    return fast, slow, b_release


async def _drain(job_lifecycle, task) -> None:
    """Let a queued done-callback fire without awaiting the task itself."""
    while not task.done():
        await asyncio.sleep(0)


async def _reap(job_lifecycle, job_id: str, b_release: asyncio.Event) -> None:
    """Release the held factory, await whatever the registry still owns, reset."""
    b_release.set()
    live = job_lifecycle.tracked_task(job_id)
    if live is not None:
        await asyncio.gather(live, return_exceptions=True)
    job_lifecycle.reset_job_runner()


async def test_a_task_that_never_gets_registered_is_still_awaitable_by_the_caller():
    """An early failure must be surfaced to the caller, never swallowed."""
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "escape-job"
    boom = RuntimeError("escaped before registration")

    async def fail_now():
        raise boom

    try:
        task = job_lifecycle.start_tracked_task(job_id, fail_now)
        result = await asyncio.gather(task, return_exceptions=True)
        assert result == [boom], "the early failure must be surfaced, not swallowed"
    finally:
        job_lifecycle.reset_job_runner()


async def test_a_failed_task_is_never_reported_as_running():
    """After an early failure the registry must not claim the job is running."""
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "failed-owner-job"

    async def fail_now():
        raise RuntimeError("early failure")

    try:
        task = job_lifecycle.start_tracked_task(job_id, fail_now)
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert job_lifecycle.is_running(job_id) is False
        assert job_lifecycle.tracked_task(job_id) is None
    finally:
        job_lifecycle.reset_job_runner()


async def test_replacement_task_registration_survives_the_predecessors_callback():
    """#448: A's queued done-callback pops the slot and evicts the live B."""
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "evict-job"
    fast, slow, b_release = _finishable_and_blocked_factories()

    try:
        a = job_lifecycle.start_tracked_task(job_id, fast)
        # Drive A to completion WITHOUT awaiting it: completion queues the
        # release callback for the next turn, while this coroutine resumes now.
        while not a.done():
            await asyncio.sleep(0)
        # Same turn as A's completion: B is created and registered.
        b = job_lifecycle.start_tracked_task(job_id, slow)
        # Next turn: A's queued release fires -- old code pops B's slot.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        live = job_lifecycle.tracked_task(job_id)
        assert live is not None and live is b, "A's stale done-callback evicted B's registration (#448)"
        assert job_lifecycle.is_running(job_id)
    finally:
        await _reap(job_lifecycle, job_id, b_release)


async def test_the_registry_slot_is_only_reclaimed_by_its_own_task():
    """Companion invariant: only the registered task's own completion reclaims."""
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "slot-owner-job"
    fast, slow, b_release = _finishable_and_blocked_factories()

    try:
        a = job_lifecycle.start_tracked_task(job_id, fast)
        while not a.done():
            await asyncio.sleep(0)
        second = job_lifecycle.start_tracked_task(job_id, slow)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        live = job_lifecycle.tracked_task(job_id)
        assert live is second, "a finished task's callback must not reclaim a successor's slot"
    finally:
        await _reap(job_lifecycle, job_id, b_release)
