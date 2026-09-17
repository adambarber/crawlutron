"""Browser worker contract: returns classified results, re-queues capacity failures."""

from collections import deque

import pytest
from _testkit import *

from app.crawl.strategies.browser_pool import BrowserPoolExhaustedError


@pytest.mark.asyncio
class TestBrowserWorkerGuard:
    """The browser worker must return a classified result, never raise.

    BrowserRenderer.render already wraps every attempt in a catch-all retry
    loop that returns a classified CrawlResult, so the raw-propagation gap is
    NOT in render -- it is in PooledBrowserWorker, which lets
    BrowserPoolExhaustedError (and a missing pool) escape to the scheduler.
    """

    async def test_pool_exhaustion_returns_result_not_raises(self):
        """A worker whose pool is exhausted must return a classified result so
        the at-least-once layer can re-queue it -- not raise to the scheduler."""
        from app.crawl.strategies.browser_pool import PooledBrowserWorker
        from app.models import CrawlJob, RenderOptions

        class ExhaustedPool:
            async def acquire(self):
                raise BrowserPoolExhaustedError("no renderer")

            def release(self, renderer):
                pass

        worker = PooledBrowserWorker(ExhaustedPool())
        job = CrawlJob(
            index=0,
            url="https://a.example/x",
            domain="a.example",
            options=RenderOptions(timeout_seconds=1),
        )
        result = await worker(job)

        assert result.status == "failed"
        assert result.classification == "browser_pool_exhausted"
        assert (result.diagnostics or {}).get("retryable") is True


@pytest.mark.asyncio
class TestPoolExhaustionRequeue:
    """A capacity-exhausted URL goes back on the frontier, once."""

    async def test_pool_exhausted_url_is_requeued_to_the_frontier_tail(self):
        """The at-least-once contract for capacity failures: the URL was never
        attempted, so recording it as a permanent failure loses work. The
        recursive loop must re-queue it at the frontier tail under a per-job
        cap so a permanently oversubscribed crawl degrades instead of spinning."""
        from app.crawl.recursive import MAX_POOL_EXHAUSTION_REQUEUES, requeue_pool_exhausted
        from app.models import CrawlTask

        task = CrawlTask(index=0, original_url="http://a.example/x", normalized_url="http://a.example/x", domain="a.example")
        frontier = deque()

        # A first exhaustion is re-queued to the tail.
        assert requeue_pool_exhausted(task, depth=1, frontier=frontier, attempts={}) is True
        assert len(frontier) == 1
        assert frontier[0][0] == 1

        # The cap bounds it: after MAX_POOL_EXHAUSTION_REQUEUES attempts the URL
        # is allowed to fail permanently rather than spin forever.
        attempts = {task.normalized_url: MAX_POOL_EXHAUSTION_REQUEUES}
        assert requeue_pool_exhausted(task, depth=1, frontier=frontier, attempts=attempts) is False
        assert len(frontier) == 1, "a capped URL must not be re-queued again"
