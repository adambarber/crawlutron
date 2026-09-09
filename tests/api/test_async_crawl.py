"""Tests for async single-shot crawl jobs (Option 3 / Item 3)."""

import asyncio

import pytest
from helpers.server import WEBHOOK_RECEIVED

from app.constants import HTTP_DECLARED_CRAWLER


async def _poll_until(predicate, *, timeout: float = 10.0, interval: float = 0.1):
    """Poll ``predicate`` until it returns truthy or ``timeout`` expires.

    Replaces the previous ``for _ in range(20): await asyncio.sleep(0.5)``
    pattern: faster minimum polling, real timeout via ``asyncio.wait_for``,
    and a single ``pytest.fail`` site that names the failing condition.
    Predicates can be sync or async; async predicates are awaited.
    """

    async def _runner() -> bool:
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            value = predicate()
            if asyncio.iscoroutine(value):
                value = await value
            if value:
                return True
            if asyncio.get_event_loop().time() >= deadline:
                return False
            await asyncio.sleep(interval)

    try:
        ok = await asyncio.wait_for(_runner(), timeout=timeout + 1.0)
    except TimeoutError:
        ok = False
    return ok


class TestAsyncCrawlApi:
    """Tests for async_mode on /v1/crawl endpoint."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()

    def _crawl_payload(self, **extra):
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": [HTTP_DECLARED_CRAWLER],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
        }
        payload.update(extra)
        return payload

    async def test_v1_crawl_async_mode_returns_202(self):
        """async_mode=true returns 202 with request_id and status_url."""
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(async_mode=True))
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body
        assert body["request_id"].startswith("crawl_")
        assert "status_url" in body
        assert body["status_url"] == f"/v1/crawl/{body['request_id']}"
        assert body["webhook_url"] is None

    async def test_v1_crawl_async_mode_status_url_returns_202_then_200(self):
        """Polling status_url returns 202 (pending) then 200 (completed)."""
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(async_mode=True))
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        async def _poll_status() -> bool:
            status_resp = await self.client.get(f"/v1/crawl/{request_id}")
            if status_resp.status_code == 200:
                body = status_resp.json()
                assert body["success"] is True
                assert len(body["results"]) == 1
                assert body["results"][0]["status"] == "success"
                return True
            assert status_resp.status_code == 202, (
                f"unexpected status {status_resp.status_code}: {status_resp.text}"
            )
            data = status_resp.json()
            assert data["code"] == "pending"
            assert "status" in data["details"]
            return False

        assert await _poll_until(_poll_status, timeout=10.0, interval=0.1), (
            "Timed out waiting for async crawl to complete"
        )

    async def test_v1_crawl_async_mode_with_webhook_delivers(self):
        """async_mode with webhook_url delivers result to webhook on completion."""
        payload = self._crawl_payload(
            async_mode=True,
            webhook_url=f"{self.base}/webhook_sink",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        # Wait for webhook delivery (faster polling, real timeout).
        assert await _poll_until(lambda: len(WEBHOOK_RECEIVED) == 1, timeout=10.0, interval=0.1), (
            "Webhook not delivered in time"
        )

        delivered = delivered = __import__("json").loads(WEBHOOK_RECEIVED[0].decode("utf-8"))
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
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(async_mode=True))
        assert resp.status_code == 202
        request_id = resp.json()["request_id"]

        # Wait for completion (faster polling, real timeout).
        async def _wait_done() -> bool:
            status_resp = await self.client.get(f"/v1/crawl/{request_id}")
            return status_resp.status_code == 200

        assert await _poll_until(_wait_done, timeout=10.0, interval=0.1), (
            "Async crawl did not complete in time"
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
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(async_mode=False))
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
        payload = self._crawl_payload(
            urls=[f"{self.base}/", f"{self.base}/no-such-page-xyz"],
            async_mode=True,
        )
        payload.pop("url")
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        async def _wait_done() -> bool:
            status_resp = await self.client.get(f"/v1/crawl/{request_id}")
            return status_resp.status_code == 200

        assert await _poll_until(_wait_done, timeout=15.0, interval=0.1), (
            "Async crawl did not complete in time"
        )
        job = job_store.load(request_id)
        assert job.stats["total_failed"] == 1
        assert job.stats["total_crawled"] == 2

    async def test_v1_crawl_async_mode_batch_urls(self):
        """async_mode works with batch urls."""
        payload = self._crawl_payload(
            urls=[f"{self.base}/", f"{self.base}/page2"],
            async_mode=True,
        )
        payload.pop("url")
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        request_id = resp.json()["request_id"]

        # Poll until completed.
        async def _wait_done() -> bool:
            status_resp = await self.client.get(f"/v1/crawl/{request_id}")
            if status_resp.status_code == 200:
                body = status_resp.json()
                assert body["submitted_count"] == 2
                assert len(body["results"]) == 2
                return True
            assert status_resp.status_code == 202, f"Unexpected status: {status_resp.status_code}"
            return False

        assert await _poll_until(_wait_done, timeout=15.0, interval=0.1), (
            "Timed out waiting for async batch crawl to complete"
        )

    async def test_v1_crawl_webhook_backward_compat(self):
        """webhook_url without async_mode still works (backward compat)."""
        payload = self._crawl_payload(
            webhook_url=f"{self.base}/webhook_sink",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"
        assert "request_id" in body

        # Wait for webhook.
        assert await _poll_until(lambda: len(WEBHOOK_RECEIVED) == 1, timeout=10.0, interval=0.1), (
            "Webhook not delivered in time"
        )

        delivered = __import__("json").loads(WEBHOOK_RECEIVED[0].decode("utf-8"))
        assert delivered["success"] is True

    async def test_v1_crawl_async_mode_invalid_webhook_rejected(self):
        """Invalid webhook_url is rejected at accept time."""
        payload = self._crawl_payload(
            async_mode=True,
            webhook_url="not-a-valid-url",
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 422
        assert resp.json()["code"] == "validation_error"

    async def test_v1_crawl_async_mode_concurrent_limit(self):
        """Concurrent async crawls are bounded by semaphore."""
        # Fire multiple async crawls
        request_ids = []
        for _ in range(3):
            resp = await self.client.post("/v1/crawl", json=self._crawl_payload(async_mode=True))
            assert resp.status_code == 202
            request_ids.append(resp.json()["request_id"])

        # All should eventually complete.
        async def _wait_all() -> bool:
            for request_id in request_ids:
                status_resp = await self.client.get(f"/v1/crawl/{request_id}")
                if status_resp.status_code == 200:
                    body = status_resp.json()
                    assert body["success"] is True
                    continue
                if status_resp.status_code != 202:
                    pytest.fail(f"Unexpected status: {status_resp.status_code}")
                return False
            return True

        assert await _poll_until(_wait_all, timeout=15.0, interval=0.1), (
            "Async concurrent-limit test did not complete in time"
        )


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
            store = JobStore(f"{tmpdir}/test.db")
            job = store.create_single_shot("test-id", "http://example.com/", {"test": "policy"})
            assert job.job_id == "test-id"
            assert job.kind == JobKind.SINGLE_SHOT
            assert job.root_url == "http://example.com/"
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
            store = JobStore(f"{tmpdir}/test.db")
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
            job.status = JobStatus.COMPLETED
            store.save(job)

            # Load and verify
            loaded = store.load("test-id")
            assert loaded is not None
            assert loaded.kind == JobKind.SINGLE_SHOT
            assert loaded.status == JobStatus.COMPLETED
            assert len(loaded.results) == 1
            assert loaded.results[0]["request_id"] == "test-id"
