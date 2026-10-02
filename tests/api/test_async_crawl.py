"""Tests for async single-shot crawl jobs."""

import asyncio

from app.api import job_runner
from tests._assertions import accepted_crawl, completed_crawl, poll_webhook_delivery, submit_async_crawl
from tests._async_harnesses import CountingSemaphore
from tests._async_harnesses import crawl_api as _crawl_api
from tests._fetcher_factories import make_crawl_payload
from tests._job_harnesses import loaded_job, single_shot_job
from tests.helpers.server import WEBHOOK_RECEIVED

# The client + origin + cleared webhook sink come from the shared fixture, so
# this module does not re-declare the arrangement in a `setup` method.
#
# The alias-then-rebind is not ceremony. Every test below takes the fixture as a
# parameter named `crawl_api`, so binding that name at module scope collides with
# each of them and F811 rejects the import outright. Binding under a name nothing
# shadows, then publishing it once, is what keeps the fixture resolvable here.
# The alternative is to move the fixture into ``conftest.py``, which is worth
# doing the moment a second module wants it -- and not before, because until then
# it would put a fixture used by one file into the global namespace of all of
# them.
crawl_api = _crawl_api


class TestAsyncCrawlApi:
    """Tests for async_mode on /v1/crawl endpoint."""

    async def test_v1_crawl_async_mode_returns_202(self, crawl_api):
        """Async_mode=true returns 202 with request_id and status_url."""
        body = await accepted_crawl(crawl_api.client, crawl_api.payload(async_mode=True))
        assert "request_id" in body
        assert body["request_id"].startswith("crawl_")
        assert "status_url" in body
        assert body["status_url"] == f"/v1/crawl/{body['request_id']}"
        assert body["webhook_url"] is None

    async def test_v1_crawl_async_mode_over_batch_size_returns_422(self, crawl_api):
        """An oversized batch is rejected at accept-time, never 202'd then failed async."""
        from app.settings import CrawlerSettings

        urls = [f"{crawl_api.base}/{i}" for i in range(CrawlerSettings.from_mapping().max_batch_size + 1)]
        resp = await crawl_api.client.post("/v1/crawl", json={"urls": urls, "async_mode": True})
        assert resp.status_code == 422, resp.text
        assert "batch_size_exceeded" in resp.text

    async def test_v1_crawl_rejects_policy_blocked_async_target_before_acceptance(
        self, crawl_api, override_settings
    ):
        from app.main import job_store

        override_settings(denied_hosts="blocked.test")
        response = await crawl_api.client.post(
            "/v1/crawl",
            # Not the helper server's root: the subject here is a host the
            # policy denies, so the payload names it directly.
            json=make_crawl_payload("https://blocked.test/secret", async_mode=True),
        )

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert response.json()["details"]["errors"][0]["msg"] == "host_denied"
        # Scoped to the blocked host rather than to a count of every job in the
        # store: job_store is a process-global, and a background job submitted
        # by another test can register between two reads of it, which made this
        # assertion fail only under some xdist distributions. The claim under
        # test is "this blocked target created no job", so ask exactly that.
        assert not [
            job for job in job_store.list_jobs() if job["root_url"].startswith("https://blocked.test")
        ], "a policy-blocked target created a job"

    async def test_v1_crawl_async_mode_status_url_returns_202_then_200(self, crawl_api):
        """Polling status_url returns 202 (pending) then 200 (completed)."""

        def check_pending(response):
            assert response.status_code == 202, response.text
            data = response.json()
            assert data["code"] == "pending"
            assert "status" in data["details"]

        request_id, response = await submit_async_crawl(
            crawl_api.client,
            crawl_api.payload(async_mode=True),
            message="Timed out waiting for async crawl to complete",
            on_pending=check_pending,
        )
        body = response.json()
        assert body["success"] is True
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "success"

    async def test_single_shot_stats_do_not_fabricate_discovered(self, crawl_api):
        """Single-shot jobs never enqueue a frontier, so "discovered" is
        undefined for them -- stats must report what actually happened
        (submitted pages crawled/failed), never invent a discovered count
        that happens to equal crawled. Whole-site jobs keep the frontier-
        derived total_discovered; a conflated value makes the two job kinds
        mean different things under one field name.
        """
        from app.main import job_store

        request_id, _ = await submit_async_crawl(crawl_api.client, crawl_api.payload(async_mode=True))

        row = job_store.load(request_id)
        assert row is not None
        assert "total_discovered" not in row.stats
        assert row.stats["total_crawled"] == 1
        assert row.stats["total_failed"] == 0

    async def test_v1_crawl_async_mode_with_webhook_delivers(self, crawl_api):
        """Async_mode with webhook_url delivers result to webhook on completion."""
        resp = await crawl_api.client.post(
            "/v1/crawl",
            json=crawl_api.payload(async_mode=True, webhook_url=f"{crawl_api.base}/webhook_sink"),
        )
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        delivered = await poll_webhook_delivery(WEBHOOK_RECEIVED)
        assert delivered["request_id"] == request_id
        assert delivered["success"] is True
        assert len(delivered["results"]) == 1

        # Verify status_url also works after webhook delivery
        status_resp = await crawl_api.client.get(f"/v1/crawl/{request_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["success"] is True

    async def test_v1_crawl_async_mode_results_persisted_in_job_store(self, crawl_api):
        """Async crawl results are persisted in job_store (single source of truth)."""
        request_id, _ = await submit_async_crawl(crawl_api.client, crawl_api.payload(async_mode=True))

        # Verify job exists in job_store with SINGLE_SHOT kind
        from app.main import job_store

        job = job_store.load(request_id)
        assert job is not None, "Job not found in job_store"
        assert job.kind.value == "single_shot"
        assert job.status.value == "completed"
        assert len(job.results) == 1
        assert job.results[0]["request_id"] == request_id

    async def test_v1_crawl_async_mode_404_for_unknown_request_id(self, crawl_api):
        """Status_url returns 404 for unknown request_id."""
        resp = await crawl_api.client.get("/v1/crawl/crawl_unknown123")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_v1_crawl_sync_mode_still_works(self, crawl_api):
        """Default sync mode (async_mode=false) still returns results directly."""
        body = await completed_crawl(crawl_api.client, crawl_api.payload(async_mode=False))
        assert body["success"] is True
        assert len(body["results"]) == 1
        assert "request_id" in body

    async def test_v1_crawl_async_mode_counts_failed_results(self, crawl_api):
        """Job stats must count failed results, not hardcode zero."""
        from app.main import job_store

        # One page fetches fine, one 404s (failed). A dead host would not
        # work here: robots fetch errors fail closed to
        # robots_disallowed/skipped instead of reaching the fetch.
        request_id, _ = await submit_async_crawl(
            crawl_api.client,
            crawl_api.batch_payload(["/", "/no-such-page-xyz"], async_mode=True),
        )
        job = loaded_job(job_store, request_id)
        assert job.stats["total_failed"] == 1
        assert job.stats["total_crawled"] == 2

    async def test_v1_crawl_async_mode_batch_urls(self, crawl_api):
        """Async_mode works with batch urls."""
        request_id, response = await submit_async_crawl(
            crawl_api.client,
            crawl_api.batch_payload(["/", "/page2"], async_mode=True),
            message="Timed out waiting for async batch crawl to complete",
        )
        body = response.json()
        assert body["submitted_count"] == 2
        assert len(body["results"]) == 2

    async def test_v1_crawl_webhook_backward_compat(self, crawl_api):
        """Webhook_url without async_mode still works (backward compat)."""
        payload = crawl_api.payload(webhook_url=f"{crawl_api.base}/webhook_sink")
        body = await accepted_crawl(crawl_api.client, payload)
        assert "request_id" in body

        delivered = await poll_webhook_delivery(WEBHOOK_RECEIVED)
        assert delivered["success"] is True

    async def test_v1_crawl_async_mode_invalid_webhook_rejected(self, crawl_api):
        """Invalid webhook_url is rejected at accept time."""
        resp = await crawl_api.client.post(
            "/v1/crawl",
            json=crawl_api.payload(async_mode=True, webhook_url="not-a-valid-url"),
        )
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    async def test_v1_crawl_async_mode_concurrent_limit(self, crawl_api, monkeypatch):
        """Concurrent async crawls queue on the bound rather than all running.

        The bound is asserted, not assumed. A counting semaphore of width one
        is installed in place of the runner's, so the test observes how many
        crawls were inside the bound at once rather than inferring it from the
        fact that the submissions returned.

        That is what the previous version of this test failed to do. It
        submitted three crawls against the default limit of four -- under the
        limit, so no queueing happened -- and then asserted only that all
        three succeeded. An implementation with no bound at all passed it.

        The peak is also the falsifiable half: it reads 1 only because the
        crawler entered the semaphore, so removing the `async with` drops it to
        0, and a width other than 1 pushes it above 1. Both are caught without
        a slow page, a sleep, or a wall clock anywhere in the test.
        """
        from app.api import async_crawl_runner

        counter = CountingSemaphore(1)
        monkeypatch.setattr(async_crawl_runner, "_build_async_crawl_semaphore", lambda: counter)
        # The runner caches one semaphore per event loop and only rebuilds it
        # when the loop changes. Clearing the cache is what makes the patch
        # visible to the first crawl of this test rather than the second.
        monkeypatch.setattr(async_crawl_runner, "_async_crawl_semaphore", None, raising=False)
        monkeypatch.setattr(async_crawl_runner, "_async_crawl_semaphore_loop", None, raising=False)

        payload = crawl_api.payload(async_mode=True)
        completed = await asyncio.gather(*(submit_async_crawl(crawl_api.client, payload) for _ in range(3)))

        assert counter.peak_holders == 1, f"bound not honoured: peak {counter.peak_holders} concurrent crawls"
        for _request_id, response in completed:
            assert response.status_code == 200
            assert response.json()["success"] is True


class TestAsyncCrawlJobStoreIntegration:
    """Tests verifying job_store integration for async crawls."""

    async def test_job_store_single_shot_kind(self):
        """Job_store.create_single_shot creates job with SINGLE_SHOT kind."""
        from app.crawl.job_store import JobKind, JobStatus

        with single_shot_job() as (_store, job):
            assert job.job_id == "test-id"
            assert job.kind == JobKind.SINGLE_SHOT
            # Roots are stored in canonical page-identity form:
            # normalize_public_http_url is the single source of truth for page
            # identity, so the submitted trailing slash is normalized away.
            # Storing the raw spelling is what let one page occupy two
            # frontier/visited identities (cap underfill, duplicate crawl).
            assert job.root_url == "http://example.com"
            assert job.status == JobStatus.PENDING
            assert job.frontier == []
            assert job.visited == set()
            assert job.results == []

    async def test_job_store_single_shot_persists_results(self):
        """Results are stored in job.results for SINGLE_SHOT jobs."""
        from app.api_models import (
            CrawlStatus,
        )
        from app.constants import (
            CrawlClassification,
        )
        from app.crawl.job_store import JobKind, JobStatus
        from app.models.unified_crawl_response import UnifiedCrawlResponse
        from app.models.unified_crawl_result import UnifiedCrawlResult

        with single_shot_job() as (store, job):
            # Simulate completion
            result = UnifiedCrawlResult(
                original_url="http://example.com/",
                normalized_url="http://example.com/",
                final_url="http://example.com/",
                status=CrawlStatus.success,
                classification=CrawlClassification.rendered,
                selected_strategy="HTTP_DECLARED_CRAWLER",
            )
            response_payload = UnifiedCrawlResponse(
                request_id="test-id",
                submitted_count=1,
                policy_schema_version="2026-06-23",
                success=True,
                results=[result],
            ).model_dump()

            job.results = [response_payload]
            store.save(job)
            store.set_status("test-id", JobStatus.COMPLETED, expected=JobStatus.PENDING)

            # Load and verify
            loaded = store.load("test-id")
            assert loaded is not None
            assert loaded.kind == JobKind.SINGLE_SHOT
            assert loaded.status == JobStatus.COMPLETED
            assert len(loaded.results) == 1
            assert loaded.results[0]["request_id"] == "test-id"


class TestAsyncCrawlShutdownOwnership:
    """The async crawl path is work the job runner already owns a registry for."""

    async def test_an_in_flight_async_crawl_is_cancelled_by_the_shutdown_drain(self, crawl_api, monkeypatch):
        """A shutdown that cannot see the async crawl cannot stop it.

        The endpoint hands the crawl to a Starlette background task, which the
        runner's registry never learns about, so the drain that app/main.py
        documents as stopping the world first skips it. A crawl left running
        past shutdown is the one thing that can publish a webhook against
        dependencies the shutdown just cleared.
        """
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def blocked_crawl(*args, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        monkeypatch.setattr("app.api.async_crawl_runner.run_async_crawl", blocked_crawl)

        # A 202 is a promise that the work outlives the request. Handed to a
        # Starlette background task instead, the crawl is awaited inside the
        # request cycle, so the call does not return until the crawl is done.
        body = await asyncio.wait_for(
            accepted_crawl(crawl_api.client, crawl_api.payload(async_mode=True)),
            timeout=5,
        )
        job_id = body["request_id"]

        assert await asyncio.wait_for(started.wait(), timeout=5), "the crawl never started"
        assert job_runner.is_running(job_id), "an in-flight async crawl is invisible to the runner"

        await job_runner.shutdown_job_runner()
        assert await asyncio.wait_for(cancelled.wait(), timeout=5), "shutdown did not stop the crawl"
        assert not job_runner.is_running(job_id)
