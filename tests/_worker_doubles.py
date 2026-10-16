"""Worker dispatch: the two shapes ``call_worker`` accepts, and the lie it names --
five tests across two modules re-derived this contract with untyped stand-ins, so
these are the same claims stated once on the real models.
"""

from __future__ import annotations

from typing import cast

import pytest

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import CrawlResult
from app.models.crawl_worker import SyncCrawlWorker
from app.orchestration.worker import call_worker
from tests._result_builders import crawl_result, make_job

WORKER_URL = "http://worker.test/page"


async def assert_coroutine_worker_is_awaited_directly() -> None:
    """A coroutine worker is awaited and its own result object comes back, asserted by
    identity -- the claim is ``call_worker`` returns the object the worker returned,
    not a copy or re-read, which a value comparison would not catch.
    """
    job = make_job(0, WORKER_URL)
    expected = crawl_result(WORKER_URL)
    seen: list[CrawlJob] = []

    async def worker(received: CrawlJob) -> CrawlResult:
        seen.append(received)
        return expected

    assert await call_worker(worker, job) is expected
    assert seen == [job], f"the worker saw {seen}, not the job it was dispatched with"


async def assert_sync_worker_runs_once_in_a_thread() -> None:
    """A plain worker runs in a thread, once, and its result survives the hop; the call
    count matters because a plain function awaited in-loop blocks the event loop, and
    a dropped result looks like a worker that never ran.
    """
    job = make_job(0, WORKER_URL)
    expected = crawl_result(WORKER_URL, domain="worker.test")
    seen: list[CrawlJob] = []

    def worker(received: CrawlJob) -> CrawlResult:
        seen.append(received)
        return expected

    result = await call_worker(worker, job)
    assert len(seen) == 1, f"sync worker invoked {len(seen)}x (side effects kept, result discarded)"
    assert seen == [job]
    assert result is expected


async def assert_sync_worker_returning_awaitable_is_named() -> None:
    """A plain function returning a coroutine is refused at the seam, by name, because
    ``lambda job: render(job)`` once handed the sync branch an un-awaited coroutine
    that read as a ``network_error`` far downstream -- a report about the wrong thing.
    """
    job = make_job(0, WORKER_URL)

    async def render(received: CrawlJob) -> CrawlResult:
        return crawl_result(received.url)

    mistaken = cast("SyncCrawlWorker", lambda received: render(received))
    with pytest.raises(TypeError, match="returned an awaitable"):
        await call_worker(mistaken, job)
