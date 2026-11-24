"""The crawl_api drain retires the task, not just the job's status row."""

import asyncio
import contextlib
import gc
import io

import pytest

from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.service import job_lifecycle
from crawlutron.storage.job_store import JobStatus
from crawlutron.storage.safe_db import allow_on_loop
from tests._app_factory import service_app

pytestmark = pytest.mark.real_sleeps


async def _stubborn_crawl():
    """Swallow two cancellations then exit, outliving a short stop grace."""
    ignored = 0
    slept = 0
    while slept < 2 and ignored < 2:
        try:
            await asyncio.sleep(0.6)
            slept += 1
        except asyncio.CancelledError:
            ignored += 1
    return None


def _hanging_row(url: str) -> str:
    """Plant a RUNNING job row in the singleton store for the drain to find."""
    job_store = service_app().state.job_store
    with allow_on_loop():
        job = job_store.create(url, CrawlPolicy())
        job_store.set_status(job.job_id, JobStatus.RUNNING, expected=JobStatus.PENDING)
    return job.job_id


def _plant_task(job_id: str) -> None:
    """Register the stuck crawl as the job's live runner task."""
    job_lifecycle.start_tracked_task(job_id, _stubborn_crawl)


async def test_drain_cancels_and_awaits_the_live_task():
    from tests._async_harnesses import _drain_running_crawls

    job_id = _hanging_row("https://drain-task.test/")
    _plant_task(job_id)
    await asyncio.sleep(0.01)
    task = job_lifecycle.tracked_task(job_id)
    assert task is not None, "precondition: the stuck crawl is registered"
    try:
        await _drain_running_crawls(guard=0.05)

        assert task.done(), "the drain changed the row but left the crawl task running"
        assert not job_lifecycle.is_running(job_id)
        job_store = service_app().state.job_store
        assert (await job_store.aload(job_id)).status is JobStatus.CANCELLED
    finally:
        if not task.done():
            job_lifecycle.cancel_job_task(job_id)
            with contextlib.suppress(asyncio.CancelledError):
                await task


def test_loop_teardown_has_no_pending_task_after_drain():
    from tests._async_harnesses import _drain_running_crawls

    job_id = _hanging_row("https://drain-loop.test/")

    async def scenario():
        _plant_task(job_id)
        await asyncio.sleep(0.01)
        await _drain_running_crawls(guard=0.05)
        return job_lifecycle.tracked_task(job_id)

    loop = asyncio.new_event_loop()
    err = io.StringIO()
    live = None
    with contextlib.redirect_stderr(err):
        try:
            live = loop.run_until_complete(asyncio.wait_for(scenario(), timeout=3.0))
            left_pending = live is not None and not live.done()
        finally:
            # A red run must still hand the shared registry a task on this loop.
            for _ in range(2):
                if live is None or live.done():
                    break
                job_lifecycle.cancel_job_task(job_id)
                with contextlib.suppress(asyncio.CancelledError):
                    loop.run_until_complete(live)
            loop.close()
            gc.collect()
    assert "Task was destroyed but it is pending" not in err.getvalue()
    assert not left_pending, "the drain left the crawl task pending at loop teardown"
