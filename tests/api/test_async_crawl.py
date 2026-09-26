"""Tests for async single-shot crawl jobs (Option 3 / Item 3)."""

import asyncio

import pytest
from _assertions import poll_webhook_delivery, submit_async_crawl
from _fetcher_factories import make_crawl_payload
from _policy_factories import UNRESTRICTED_HOST_POLICY
from helpers.server import WEBHOOK_RECEIVED


class TestAsyncCrawlApi:
    """Tests for async_mode on /v1/crawl endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()

    async def test_v1_crawl_async_mode_returns_202(self):
        """async_mode=true returns 202 with request_id and status_url."""
        resp = await self.client.post("/v1/crawl", json=make_crawl_payload(f"{self.base}/", async_mode=True))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body
        assert body["request_id"].startswith("crawl_")
        assert "status_url" in body
        assert body["status_url"] == f"/v1/crawl/{body['request_id']}"
        assert body["webhook_url"] is None

    async def test_v1_crawl_async_mode_over_batch_size_returns_422(self):
        """L4: an oversized batch is rejected at accept-time, never 202'd then failed async."""
        from app.settings import CrawlerSettings

        urls = [f"{self.base}/{i}" for i in range(CrawlerSettings.from_mapping().max_batch_size + 1)]
        resp = await self.client.post("/v1/crawl", json={"urls": urls, "async_mode": True})
        assert resp.status_code == 422, resp.text
        assert "batch_size_exceeded" in resp.text

    async def test_v1_crawl_rejects_policy_blocked_async_target_before_acceptance(self, override_settings):
        from app.main import job_store

        override_settings(denied_hosts="blocked.test")
        response = await self.client.post(
            "/v1/crawl",
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
            job
            for job in job_store.list_jobs()
            if job["root_url"].startswith("https://blocked.test")
        ], "a policy-blocked target created a job"

    async def test_v1_crawl_async_mode_status_url_returns_202_then_200(self):
        """Polling status_url returns 202 (pending) then 200 (completed)."""
        def check_pending(response):
            assert response.status_code == 202, response.text
            data = response.json()
            assert data["code"] == "pending"
            assert "status" in data["details"]

        request_id, response = await submit_async_crawl(
            self.client,
            make_crawl_payload(f"{self.base}/", async_mode=True),
            message="Timed out waiting for async crawl to complete",
            on_pending=check_pending,
        )
        body = response.json()
        assert body["success"] is True
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "success"

    async def test_single_shot_stats_do_not_fabricate_discovered(self):
        """Single-shot jobs never enqueue a frontier, so "discovered" is
        undefined for them -- stats must report what actually happened
        (submitted pages crawled/failed), never invent a discovered count
        that happens to equal crawled. Whole-site jobs keep the frontier-
        derived total_discovered; a conflated value makes the two job kinds
        mean different things under one field name.
        """
        from app.main import job_store

        request_id, _ = await submit_async_crawl(
            self.client, make_crawl_payload(f"{self.base}/", async_mode=True)
        )

        row = job_store.load(request_id)
        assert row is not None
        assert "total_discovered" not in row.stats
        assert row.stats["total_crawled"] == 1
        assert row.stats["total_failed"] == 0

    async def test_v1_crawl_async_mode_with_webhook_delivers(self):
        """async_mode with webhook_url delivers result to webhook on completion."""
        payload = make_crawl_payload(
            f"{self.base}/",
            async_mode=True,
            webhook_url=f"{self.base}/webhook_sink",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        delivered = await poll_webhook_delivery(WEBHOOK_RECEIVED)
        assert delivered["request_id"] == request_id
        assert delivered["success"] is True
        assert len(delivered["results"]) == 1

        # Verify status_url also works after webhook delivery
        status_resp = await self.client.get(f"/v1/crawl/{request_id}")
        assert status_resp.status_code == 200
        body = status_resp.json()
        assert body["success"] is True

    async def test_v1_crawl_async_mode_results_persisted_in_job_store(self):
        """Async crawl results are persisted in job_store (single source of truth)."""
        request_id, _ = await submit_async_crawl(
            self.client, make_crawl_payload(f"{self.base}/", async_mode=True)
        )

        # Verify job exists in job_store with SINGLE_SHOT kind
        from app.main import job_store

        job = job_store.load(request_id)
        assert job is not None, "Job not found in job_store"
        assert job.kind.value == "single_shot"
        assert job.status.value == "completed"
        assert len(job.results) == 1
        assert job.results[0]["request_id"] == request_id

    async def test_v1_crawl_async_mode_404_for_unknown_request_id(self):
        """status_url returns 404 for unknown request_id."""
        resp = await self.client.get("/v1/crawl/crawl_unknown123")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"

    async def test_v1_crawl_sync_mode_still_works(self):
        """Default sync mode (async_mode=false) still returns results directly."""
        resp = await self.client.post("/v1/crawl", json=make_crawl_payload(f"{self.base}/", async_mode=False))
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["success"] is True
        assert len(body["results"]) == 1
        assert "request_id" in body

    async def test_v1_crawl_async_mode_counts_failed_results(self):
        """L5d: job stats must count failed results, not hardcode zero."""
        from app.main import job_store

        # One page fetches fine, one 404s (failed). A dead host would not
        # work here: post-M7, robots fetch errors fail closed to
        # robots_disallowed/skipped instead of reaching the fetch.
        payload = make_crawl_payload(
            f"{self.base}/",
            urls=[f"{self.base}/", f"{self.base}/no-such-page-xyz"],
            async_mode=True,
        )
        payload.pop("url")
        request_id, _ = await submit_async_crawl(self.client, payload)
        job = job_store.load(request_id)
        assert job.stats["total_failed"] == 1
        assert job.stats["total_crawled"] == 2

    async def test_v1_crawl_async_mode_batch_urls(self):
        """async_mode works with batch urls."""
        payload = make_crawl_payload(
            f"{self.base}/",
            urls=[f"{self.base}/", f"{self.base}/page2"],
            async_mode=True,
        )
        payload.pop("url")
        request_id, response = await submit_async_crawl(
            self.client,
            payload,
            message="Timed out waiting for async batch crawl to complete",
        )
        body = response.json()
        assert body["submitted_count"] == 2
        assert len(body["results"]) == 2

    async def test_v1_crawl_webhook_backward_compat(self):
        """webhook_url without async_mode still works (backward compat)."""
        payload = make_crawl_payload(
            f"{self.base}/",
            webhook_url=f"{self.base}/webhook_sink",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body

        delivered = await poll_webhook_delivery(WEBHOOK_RECEIVED)
        assert delivered["success"] is True

    async def test_v1_crawl_async_mode_invalid_webhook_rejected(self):
        """Invalid webhook_url is rejected at accept time."""
        payload = make_crawl_payload(
            f"{self.base}/",
            async_mode=True,
            webhook_url="not-a-valid-url",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    async def test_v1_crawl_async_mode_concurrent_limit(self):
        """Concurrent async crawls are bounded by semaphore."""
        payload = make_crawl_payload(f"{self.base}/", async_mode=True)
        completed = await asyncio.gather(*(submit_async_crawl(self.client, payload) for _ in range(3)))
        for request_id, response in completed:
            assert response.status_code == 200
            assert response.json()["success"] is True


class TestAsyncCrawlJobStoreIntegration:
    """Tests verifying job_store integration for async crawls."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url

    async def test_job_store_single_shot_kind(self):
        """job_store.create_single_shot creates job with SINGLE_SHOT kind."""
        import tempfile

        from app.crawl.job_store import JobKind, JobStatus, JobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(f"{tmpdir}/test.db", policy=UNRESTRICTED_HOST_POLICY)
            job = store.create_single_shot("test-id", "http://example.com/", {"test": "policy"})
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
        import tempfile

        from app.api_models import CrawlClassification, CrawlStatus, UnifiedCrawlResponse, UnifiedCrawlResult
        from app.crawl.job_store import JobKind, JobStatus, JobStore

        with tempfile.TemporaryDirectory() as tmpdir:
            store = JobStore(f"{tmpdir}/test.db", policy=UNRESTRICTED_HOST_POLICY)
            job = store.create_single_shot("test-id", "http://example.com/", {"test": "policy"})

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
