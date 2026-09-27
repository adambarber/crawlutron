"""Browser worker contract: returns classified results, re-queues capacity failures."""

from collections import deque

import pytest
from _assertions import assert_result_failed
from _policy_factories import UNRESTRICTED_HOST_POLICY, crawl_policy

from app.constants import BROWSER_HEADLESS
from app.crawl.strategies.browser_pool import BrowserPoolExhaustedError


class ExhaustedPool:
    """A pool with no renderer: ``acquire`` always raises, the release path is inert.

    The pool interface the worker reads on its failure path, and nothing else --
    the worker under test is real, so a fake here that grew a method would be
    modelling the production pool rather than the failure.
    """

    browser_strategy = BROWSER_HEADLESS

    async def acquire(self):
        raise BrowserPoolExhaustedError("no renderer")

    def release(self, renderer):
        pass


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

        worker = PooledBrowserWorker(ExhaustedPool())
        job = CrawlJob(
            index=0,
            url="https://a.example/x",
            domain="a.example",
            options=RenderOptions(timeout_seconds=1),
            host_policy=UNRESTRICTED_HOST_POLICY,
        )
        result = await worker(job)

        assert result.status == "failed"
        assert result.classification == "browser_pool_exhausted"
        # The CrawlResult carries the retryable flag in its metadata (the
        # engine path's equivalent lives in EngineCrawlResult.diagnostics);
        # the render path surfaces it straight into the response diagnostics.
        assert (result.metadata or {}).get("retryable") is True


@pytest.mark.asyncio
class TestTransientRequeue:
    """A capacity-exhausted URL goes back on the frontier, once."""

    async def test_pool_exhausted_url_is_requeued_to_the_frontier_tail(self):
        """The at-least-once contract for capacity failures: the URL was never
        attempted, so recording it as a permanent failure loses work. The
        recursive loop must re-queue it at the frontier tail under a per-job
        cap so a permanently oversubscribed crawl degrades instead of spinning."""
        from app.crawl.recursive import MAX_TRANSIENT_REQUEUES, requeue_transient
        from app.models import CrawlTask

        task = CrawlTask(
            index=0,
            original_url="http://a.example/x",
            normalized_url="http://a.example/x",
            domain="a.example",
        )
        frontier = deque()

        # A first exhaustion is re-queued to the tail.
        assert requeue_transient(task, depth=1, frontier=frontier, attempts={}) is True
        assert len(frontier) == 1
        assert frontier[0][0] == 1

        # The cap bounds it: after MAX_TRANSIENT_REQUEUES attempts the URL
        # is allowed to fail permanently rather than spin forever.
        attempts = {task.normalized_url: MAX_TRANSIENT_REQUEUES}
        assert requeue_transient(task, depth=1, frontier=frontier, attempts=attempts) is False
        assert len(frontier) == 1, "a capped URL must not be re-queued again"

    async def test_pool_exhausted_url_is_requeued_through_the_real_crawl_path(self, base_url):
        """End-to-end proof of the at-least-once contract this cluster is about.

        An exhausted pool worker returns its classified ``CrawlResult``; the
        engine's browser consumer reads it without an AttributeError; the
        recursive loop keys on ``browser_pool_exhausted`` and re-queues the URL
        to the frontier tail under the cap. That re-queue is observable here as
        the worker being invoked once per try, and the classification surviving
        to the final result. Before the worker honored its contracted shape the
        classification was destroyed at the scheduler envelope (AttributeError
        -> network_error) and the re-queue never fired.
        """
        from _fetcher_factories import run_crawl_async

        from app.crawl.recursive import MAX_TRANSIENT_REQUEUES
        from app.crawl.strategies.browser_pool import PooledBrowserWorker

        # The real production worker, not a stand-in: only the pool is faked
        # (no browser binary in CI), so the guard, the chokepoint
        # classification, and the engine consumption all run as in production.
        pooled = PooledBrowserWorker(ExhaustedPool())
        invocations: list[str] = []

        async def browser_worker(job):
            invocations.append(job.url)
            return await pooled(job)

        payload = {
            "url": f"{base_url}/",
            # max_depth > 0 selects the recursive dispatcher, which owns the
            # pool-exhaustion frontier re-queue.
            "policy": crawl_policy([BROWSER_HEADLESS], limits={"max_depth": 1}),
        }
        response = await run_crawl_async(payload, browser_worker=browser_worker)
        assert_result_failed(response["results"][0], "browser_pool_exhausted")
        # One initial attempt plus the cap of frontier re-queues: the URL was
        # retried at least once rather than recorded as a permanent failure.
        assert len(invocations) == 1 + MAX_TRANSIENT_REQUEUES, invocations
