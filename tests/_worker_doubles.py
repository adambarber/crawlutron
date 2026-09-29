"""Worker dispatch: the two shapes ``call_worker`` accepts, and the lie it names.

``app.crawl.worker.call_worker`` is the seam between the engine and whatever
fetches a job, so its whole contract is two overloads and one runtime refusal.
Five tests across two modules were re-deriving that contract independently, with
untyped workers, a bare ``class _Job: pass`` in place of a ``CrawlJob``, and a
string in place of the result -- none of which the type checker could see, and
all of which a future edit to the seam could quietly invalidate. These are the
same three claims stated once, on the real models.
"""

from __future__ import annotations

from typing import cast

import pytest

from app.crawl.worker import call_worker
from app.models import CrawlJob, CrawlResult, SyncCrawlWorker
from tests._result_builders import crawl_result, make_job

WORKER_URL = "http://worker.test/page"


async def assert_coroutine_worker_is_awaited_directly() -> None:
    """A coroutine worker is awaited, and its own result object comes back.

    Asserted by identity, not by value: the claim is that ``call_worker`` hands
    back the object the worker returned rather than a copy or a re-read of the
    same fields, and a value comparison would pass for either of those.
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
    """A plain worker runs in a thread, once, and its result survives the hop.

    The call count is the half that matters: a plain function that is awaited
    instead of run off-loop blocks the event loop, and a result that got dropped
    on the way back looks like a worker that never ran. Both are invisible to an
    assertion about the return value alone.
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
    """A plain function returning a coroutine is refused at the seam, by name.

    This is the mistake the seam's guard exists for: a ``lambda job: render(job)``
    is a plain function, so it took the sync branch and handed the strategy an
    un-awaited coroutine, which then read ``.html`` off it and reported a
    ``network_error`` ~100 lines downstream -- a report that reads like a site
    problem and sent the investigation at the page instead of at the double.

    The mismatch cannot be written in the type system: a coroutine function
    satisfies neither overload, which is precisely why the runtime check is
    needed. So the callable is cast to the sync worker type it is being passed
    as. That narrows to a real type rather than erasing the contract, and it is
    the exact claim the ``TypeError`` then falsifies.
    """
    job = make_job(0, WORKER_URL)

    async def render(received: CrawlJob) -> CrawlResult:
        return crawl_result(received.url)

    mistaken = cast("SyncCrawlWorker", lambda received: render(received))
    with pytest.raises(TypeError, match="returned an awaitable"):
        await call_worker(mistaken, job)
