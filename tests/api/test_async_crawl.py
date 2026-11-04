"""Tests for async single-shot crawl jobs."""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from crawlutron.api import job_runner
from crawlutron.runtime.metrics import get_metrics
from tests._assertions import (
    accepted_crawl,
    completed_crawl,
    plaintext_webhook_refusal,
    poll_crawl_completion,
    poll_webhook_delivery,
    submit_async_crawl,
)
from tests._async_harnesses import crawl_api as _crawl_api
from tests._fetcher_factories import make_crawl_payload
from tests._job_harnesses import loaded_job, single_shot_job
from tests.helpers.server import WEBHOOK_RECEIVED

# The client + origin + cleared webhook sink come from the shared fixture, so this module does not re-declare
# the arrangement in a `setup` method. The alias-then-rebind is not ceremony.
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
        from crawlutron.config.settings import CrawlerSettings

        urls = [f"{crawl_api.base}/{i}" for i in range(CrawlerSettings.from_mapping().max_batch_size + 1)]
        resp = await crawl_api.client.post("/v1/crawl", json={"urls": urls, "async_mode": True})
        assert resp.status_code == 422, resp.text
        assert "batch_size_exceeded" in resp.text

    async def test_v1_crawl_rejects_policy_blocked_async_target_before_acceptance(
        self, crawl_api, override_settings
    ):
        from tests._app_factory import service_app

        job_store = service_app().state.job_store

        override_settings(denied_hosts="blocked.test")
        response = await crawl_api.client.post(
            "/v1/crawl",
            # Not the helper server's root: the subject here is a host the policy denies, so the payload names
            # it directly.
            json=make_crawl_payload("https://blocked.test/secret", async_mode=True),
        )

        assert response.status_code == 422
        assert response.json()["code"] == "validation_error"
        assert response.json()["details"]["errors"][0]["msg"] == "host_denied"
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
        """Single-shot jobs never enqueue a frontier, so ``total_discovered`` is
        undefined -- stats report what happened, never a value equal to crawled
        that conflates two job kinds under one field name.
        """
        from tests._app_factory import service_app

        job_store = service_app().state.job_store

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
        from tests._app_factory import service_app

        job_store = service_app().state.job_store

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
        from tests._app_factory import service_app

        job_store = service_app().state.job_store

        # One page fetches fine, one 404s (failed).
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

    async def test_v1_crawl_plaintext_webhook_refused(self, crawl_api, monkeypatch):
        """A plaintext http:// webhook rides the signature and payload TLS-less (#499).
        The door refuses it unless the operator opted out; this test opts out."""
        with plaintext_webhook_refusal(monkeypatch):
            resp = await crawl_api.client.post(
                "/v1/crawl",
                json=crawl_api.payload(async_mode=True, webhook_url="http://receiver.example/hook"),
            )
        assert resp.status_code == 422, resp.text
        assert resp.json()["code"] == "validation_error"

    async def test_v1_crawl_async_mode_concurrent_limit(self, crawl_api, monkeypatch):
        """The bound is asserted behaviorally: with the width knobs at one, three
        concurrent async crawls must serialize -- the page server sees at most one
        in-flight request at a time, never three overlapping fetches.
        """
        monkeypatch.setenv("CRAWLUTRON_MAX_WEBHOOK_CONCURRENCY", "1")
        async_crawl_runner = __import__("crawlutron.api.async_crawl_runner", fromlist=["x"])
        # The cached loop-bound semaphore is rebuilt from the new width.
        async_crawl_runner.reset_process_state()

        active = 0
        peak = 0
        lock = threading.Lock()

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.05)
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b"<html>slow</html>")
                finally:
                    with lock:
                        active -= 1

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            url = f"http://127.0.0.1:{server.server_port}/"
            payload = make_crawl_payload(url, async_mode=True)
            completed = await asyncio.gather(
                *(submit_async_crawl(crawl_api.client, payload) for _ in range(3))
            )

            assert peak <= 1, f"bound not honoured: peak {peak} concurrent crawls"
            for _request_id, response in completed:
                assert response.status_code == 200
                assert response.json()["success"] is True
        finally:
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()


class TestAsyncCrawlJobStoreIntegration:
    """Tests verifying job_store integration for async crawls."""

    async def test_job_store_single_shot_kind(self):
        """Job_store.create_single_shot creates job with SINGLE_SHOT kind."""
        from crawlutron.storage.job_store import JobKind, JobStatus

        with single_shot_job() as (_store, job):
            assert job.job_id == "test-id"
            assert job.kind == JobKind.SINGLE_SHOT
            # Roots are stored in canonical page-identity form: NormalizedUrl admission is the single source
            # of truth for page identity, so the submitted trailing slash is normalized away.
            assert job.root_url == "http://example.com/"
            assert job.status == JobStatus.PENDING
            assert job.frontier == []
            assert job.visited == set()
            assert job.results == []

    async def test_job_store_single_shot_persists_results(self):
        """Results are stored in job.results for SINGLE_SHOT jobs."""
        from crawlutron.models.crawl_status import CrawlStatus
        from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
        from crawlutron.models.unified_crawl_result import UnifiedCrawlResult
        from crawlutron.storage.job_store import JobKind, JobStatus
        from crawlutron.vocabulary.taxonomy import CrawlClassification

        with single_shot_job() as (store, job):
            # Simulate completion
            from tests._policy_factories import canonical

            result = UnifiedCrawlResult(
                original_url="http://example.com/",
                normalized_url=canonical("http://example.com/"),
                final_url=canonical("http://example.com/"),
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
        """A Starlette background task is invisible to the runner's registry, so the
        drain documented as stopping the world skips it -- and a crawl past
        shutdown can publish a webhook against dependencies just cleared.
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

        monkeypatch.setattr("crawlutron.api.async_crawl_runner.run_async_crawl", blocked_crawl)

        # A 202 is a promise that the work outlives the request
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


class TestAsyncCrawlTeardownIsolation:
    """A prior async test's PENDING/RUNNING rows must not bleed into the next quota.
    Anonymous concurrency counts every owned PENDING+RUNNING row, so a leftover row
    from a cancelled test can push a later test over its quota of four."""

    async def test_teardown_clears_a_stray_pending_anonymous_job(self):
        from tests._app_factory import service_app

        job_store = service_app().state.job_store
        from tests._async_harnesses import _drain_running_crawls

        job_store.create_single_shot("stray_pending", "http://example.com/", {}, owner="")
        assert job_store.active_count_for_caller("") >= 1

        # This is what the crawl_api fixture runs in its teardown.
        await _drain_running_crawls()

        assert job_store.active_count_for_caller("") == 0


class TestAsyncCrawlMetrics:
    """The async door's metrics must say what the sync door's say (#314 A6)."""

    async def test_async_crawl_records_elapsed_ms(self, crawl_api):
        """One async crawl updates ``crawler_crawl_seconds_total``: the sync path
        records elapsed through the shared completion helper, so the async path
        does not leave the duration counter frozen at zero."""
        get_metrics().reset()
        before = get_metrics().snapshot().total_elapsed_ms
        await submit_async_crawl(crawl_api.client, crawl_api.payload(async_mode=True))
        after = get_metrics().snapshot().total_elapsed_ms
        assert after > before


class TestAsyncCrawlBatchTruth:
    """A single-shot batch row must record that it is a batch (#314 A7)."""

    async def test_batch_submission_records_frontier_size(self, crawl_api):
        """Three submitted URLs are three frontier rows: the same whole-site
        batch vocabulary, so listing cannot hide the batch behind URL 1."""
        payload = crawl_api.batch_payload(["/", "/page2", "/page3"], async_mode=True)
        body = await accepted_crawl(crawl_api.client, payload)
        request_id = body["request_id"]
        response = await poll_crawl_completion(crawl_api.client, request_id)
        assert response.status_code == 200
        status = (await crawl_api.client.get(f"/v1/jobs/{request_id}")).json()
        assert status["frontier_size"] == 3
        assert status["kind"] == "single_shot"
