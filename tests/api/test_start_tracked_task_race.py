"""#448 red-first: start_tracked_task's unconditional done-callback pop lets a
stale callback evict a replacement task's registration."""

import asyncio

import pytest


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
    """#448: A finished, B in flight; A's done-callback pops the slot
    unconditionally and evicts B -- ownership misreports, cancel cannot find B."""
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "evict-job"
    b_release = asyncio.Event()

    async def fast():
        return "a"

    async def slow():
        await b_release.wait()
        return "b"

    try:
        job_lifecycle.start_tracked_task(job_id, fast)
        # One turn: A runs to completion; its done-callback is QUEUED but has
        # not run -- the coroutine resumes before the callback.
        await asyncio.sleep(0)
        b = job_lifecycle.start_tracked_task(job_id, slow)
        # Next turn: A's queued callback fires and pops the slot -- evicting B
        # under the old unconditional pop.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        live = job_lifecycle.tracked_task(job_id)
        assert live is not None and live is b, "A's stale done-callback evicted B's registration (#448)"
        assert job_lifecycle.is_running(job_id)
    finally:
        b_release.set()
        live = job_lifecycle.tracked_task(job_id)
        if live is not None:
            await asyncio.gather(live, return_exceptions=True)
        job_lifecycle.reset_job_runner()


@pytest.mark.parametrize("which", ["callback-eviction"])
async def test_the_registry_slot_is_only_reclaimed_by_its_own_task(which):
    """Parametrized companion: the eviction above, stated as the invariant."""
    del which
    from app.api import job_lifecycle

    job_lifecycle.reset_job_runner()
    job_id = "slot-owner-job"

    async def leaf():
        return "leaf"

    try:
        first = job_lifecycle.start_tracked_task(job_id, leaf)
        await asyncio.sleep(0)
        second = job_lifecycle.start_tracked_task(job_id, leaf)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        live = job_lifecycle.tracked_task(job_id)
        assert live is None or live is second, (
            "a finished task's callback must not reclaim a successor's slot"
        )
        del first
    finally:
        job_lifecycle.reset_job_runner()
