"""Browser worker contract: returns classified results, re-queues capacity failures."""

from collections import deque
from typing import TYPE_CHECKING

import pytest
from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.render_options import RenderOptions
from crawlutron.transport.browser_pool import BrowserPool, PooledBrowserWorker
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS
from tests._assertions import assert_result_failed
from tests._browser_doubles import ExhaustedPool, RaisingRendererPool
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical, crawl_policy

if TYPE_CHECKING:
    from crawlutron.models.json_value import JsonValue


def _guard_job() -> CrawlJob:
    """The job every guard test runs: index 0, one admitted URL, a one-second budget."""
    return CrawlJob(
        index=0,
        url=NormalizedUrl("https://a.example/x", policy=UNRESTRICTED_HOST_POLICY),
        options=RenderOptions(timeout_seconds=1),
        host_policy=UNRESTRICTED_HOST_POLICY,
    )


async def _run_worker(pool: BrowserPool, job: CrawlJob | None = None) -> CrawlResult:
    """The real worker over one job against ``pool``."""
    return await PooledBrowserWorker(pool)(job or _guard_job())


@pytest.mark.asyncio
class TestBrowserWorkerGuard:
    """The browser worker must return a classified result, never raise.

    ``render`` already wraps everything; the gap is in ``PooledBrowserWorker``,
    where pool exhaustion used to escape to the scheduler.
    """

    async def test_pool_exhaustion_returns_result_not_raises(self):
        """A worker whose pool is exhausted must return a classified result so
        the at-least-once layer can re-queue it -- not raise to the scheduler."""
        result = await _run_worker(ExhaustedPool())

        assert result.status == "failed"
        assert result.classification == "browser_pool_exhausted"
        # The CrawlResult carries the retryable flag in its metadata (the engine path's equivalent lives in
        # EngineCrawlResult.diagnostics); the render path surfaces it straight into the response diagnostics.
        assert (result.metadata or {}).get("retryable") is True

    async def test_worker_failure_returns_the_admitted_url_as_a_value(self):
        """The contracted url slot is a NormalizedUrl, never the page's text."""
        job = _guard_job()
        result = await _run_worker(RaisingRendererPool(), job)

        assert result.classification == "network_error"
        # The identity slot carries the admitted value through unchanged; a str here survives this call but
        # lies later, at the wire model's gate.
        assert result.url is job.url


@pytest.mark.asyncio
class TestTransientRequeue:
    """A capacity-exhausted URL goes back on the frontier, once."""

    async def test_pool_exhausted_url_is_requeued_to_the_frontier_tail(self):
        """The at-least-once contract for capacity failures: the URL was never attempted.

        So recording it as permanent loses work; the loop re-queues it at the
        frontier tail under a per-job cap so an oversubscribed crawl degrades.
        """
        from crawlutron.models.crawl_task import CrawlTask
        from crawlutron.orchestration.recursive import MAX_TRANSIENT_REQUEUES, requeue_transient

        task = CrawlTask(
            index=0,
            original_url="http://a.example/x",
            normalized_url=canonical("http://a.example/x"),
        )
        frontier: deque[tuple[int, CrawlTask]] = deque()

        # A first exhaustion is re-queued to the tail.
        assert requeue_transient(task, depth=1, frontier=frontier, attempts={}) is True
        assert len(frontier) == 1
        assert frontier[0][0] == 1

        # The cap bounds it: after MAX_TRANSIENT_REQUEUES attempts the URL is allowed to fail permanently
        # rather than spin forever.
        attempts = {task.normalized_url: MAX_TRANSIENT_REQUEUES}
        assert requeue_transient(task, depth=1, frontier=frontier, attempts=attempts) is False
        assert len(frontier) == 1, "a capped URL must not be re-queued again"

    async def test_pool_exhausted_url_is_requeued_through_the_real_crawl_path(self, base_url):
        """End-to-end proof of the at-least-once contract this cluster is about.

        Before the worker honored its shape, the classification was destroyed at
        the scheduler envelope; now the re-queue fires and survives.
        """
        from crawlutron.orchestration.recursive import MAX_TRANSIENT_REQUEUES
        from crawlutron.transport.browser_pool import PooledBrowserWorker
        from tests._fetcher_factories import run_crawl_async

        # The real production worker, not a stand-in: only the pool is faked (no browser binary in CI), so the
        # guard, the chokepoint classification, and the engine consumption all run as in production.
        pooled = PooledBrowserWorker(ExhaustedPool())
        invocations: list[str] = []

        async def browser_worker(job):
            invocations.append(job.url.raw)
            return await pooled(job)

        payload: dict[str, JsonValue] = {
            "url": f"{base_url}/",
            # max_depth > 0 selects the recursive dispatcher, which owns the pool-exhaustion frontier
            # re-queue.
            "policy": crawl_policy([BROWSER_HEADLESS], limits={"max_depth": 1}),
        }
        response = await run_crawl_async(payload, browser_worker=browser_worker)
        assert_result_failed(response["results"][0], "browser_pool_exhausted")
        # One initial attempt plus the cap of frontier re-queues: the URL was retried at least once rather
        # than recorded as a permanent failure.
        assert len(invocations) == 1 + MAX_TRANSIENT_REQUEUES, invocations
