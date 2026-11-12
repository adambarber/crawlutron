"""The webhook-crawl semaphore holds the crawl only.

Claim is post-acquire (#462); delivery is post-release (#463).
"""

import asyncio
from unittest.mock import patch

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.service.async_crawl_runner import run_async_crawl
from crawlutron.storage.job_store import JobStatus
from crawlutron.storage.result_store import CrawlResultStore
from tests._api_harnesses import admitted_webhook
from tests._api_harnesses import run_with_webhook as _run_with_webhook


def _quick_crawl_result(request_id: str) -> dict[str, object]:
    """The canned executor payload the semaphore tests crawl with."""
    return {
        "results": [],
        "request_id": request_id,
        "submitted_count": 0,
        "policy_schema_version": "",
    }


def _seed_single_shot(store, request_id: str) -> None:
    """Seed one single-shot row the semaphore tests crawl against."""
    store.create_single_shot(request_id, "http://example.com/", {"url": "http://example.com/"})


class TestSemaphoreHoldsTheCrawlOnly:
    """One placement contract: acquire, claim, crawl, release, then deliver."""

    @pytest.fixture(autouse=True)
    def _width_one(self, monkeypatch):
        """Pin the semaphore to width 1 so one held slot blocks the next crawl."""
        monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY", "1")
        from crawlutron.runtime import process_state

        process_state.reset_all_process_state()
        yield
        process_state.reset_all_process_state()

    async def test_a_queued_job_is_not_claimed_running_while_waiting_for_the_slot(self, job_store, tmp_path):
        """While the semaphore is held by another crawl, the queued row stays PENDING."""
        from crawlutron.service.async_crawl_runner import async_crawl_semaphore

        request_id = "crawl_queued_pending"
        _seed_single_shot(job_store, request_id)

        async def quick_crawl(*args, **kwargs):
            return _quick_crawl_result(request_id)

        slot = async_crawl_semaphore()
        await slot.acquire()
        try:
            runner = asyncio.create_task(
                _run_with_webhook(
                    request_id, quick_crawl, None, job_store, CrawlResultStore(tmp_path / "r.db")
                )
            )
            # The old defect claimed RUNNING before acquiring within milliseconds of
            # task start; give the queued runner that window to prove it stayed PENDING.
            for _ in range(50):
                await asyncio.sleep(0.01)
                assert job_store.load(request_id).status == JobStatus.PENDING, (
                    "a queued job claimed RUNNING before the semaphore was acquired"
                )
        finally:
            slot.release()
        await asyncio.wait_for(runner, timeout=10)

    async def test_the_webhook_delivery_runs_outside_the_crawl_semaphore(self, job_store, tmp_path):
        """A retrying webhook must not hold the crawl slot (#463)."""
        from crawlutron.service.async_crawl_runner import async_crawl_semaphore

        request_id = "crawl_delivery_outside"
        _seed_single_shot(job_store, request_id)

        async def quick_crawl(*args, **kwargs):
            return _quick_crawl_result(request_id)

        # Record, don't raise: run_async_crawl's error branch swallows anything delivery
        # raises, so an in-watcher assert would never surface.
        delivery_seen_inside = True

        async def watching_delivery(target, payload):
            nonlocal delivery_seen_inside
            delivery_seen_inside = async_crawl_semaphore().locked()

        with patch("crawlutron.service.async_crawl_runner.execute_crawl", new=quick_crawl):
            with patch("crawlutron.service.webhooks.post_webhook_guarded", new=watching_delivery):
                await run_async_crawl(
                    request_id,
                    CrawlRequest.model_validate({"url": "http://example.com/"}),
                    admitted_webhook("https://webhook.example.com/cb"),
                    CrawlerSettings.from_mapping(),
                    job_store,
                    CrawlResultStore(tmp_path / "results.db"),
                    asyncio.Event(),
                )

        assert job_store.load(request_id).status == JobStatus.COMPLETED, "delivery never ran"
        assert not delivery_seen_inside, "the webhook was delivered while still holding the crawl semaphore"

    @pytest.mark.parametrize("with_webhook", [False, True])
    async def test_a_shutdown_cancelled_crawl_is_terminalized_failed(self, job_store, tmp_path, with_webhook):
        """Mid-flight cancel writes the RUNNING->FAILED terminal, then re-raises.

        Since 3.11 CancelledError is a BaseException, an uninstrumented handler leaves the
        row RUNNING with no webhook; an admitted webhook learns the cancelled FAILED (#758).
        """

        request_id = "crawl_cancelled_shutdown"
        _seed_single_shot(job_store, request_id)

        started = asyncio.Event()
        release = asyncio.Event()
        deliveries: list[dict[str, object]] = []

        async def mid_flight_crawl(*args, **kwargs):
            started.set()
            await release.wait()  # deterministic mid-flight point inside the semaphore block
            return _quick_crawl_result(request_id)

        async def recording_webhook(target, payload):
            deliveries.append(payload)

        target = admitted_webhook("https://webhook.example.com/cb") if with_webhook else None

        with patch("crawlutron.service.async_crawl_runner.execute_crawl", new=mid_flight_crawl):
            with patch("crawlutron.service.webhooks.post_webhook_guarded", new=recording_webhook):
                runner = asyncio.create_task(
                    run_async_crawl(
                        request_id,
                        CrawlRequest.model_validate({"url": "http://example.com/"}),
                        target,
                        CrawlerSettings.from_mapping(),
                        job_store,
                        CrawlResultStore(tmp_path / "results.db"),
                        asyncio.Event(),
                    )
                )
                try:
                    await asyncio.wait_for(started.wait(), timeout=10)
                    runner.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await asyncio.wait_for(runner, timeout=10)
                finally:
                    release.set()

        job = job_store.load(request_id)
        assert job.status == JobStatus.FAILED, "a cancelled crawl must leave a terminal FAILED write"
        assert job.error == "cancelled_shutdown"
        if with_webhook:
            assert deliveries, "the cancelled FAILED terminal must announce itself via the webhook"
