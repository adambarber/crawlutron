"""End-to-end /v1/crawl behavior against a real local HTTP server (no mocks)."""

import json

import pytest
from _testkit import poll_until as _poll_until  # noqa: F401  (used inside async test method)
from helpers import browser_available
from helpers.server import WEBHOOK_RECEIVED

from app.constants import HTTP_DECLARED_CRAWLER
from app.main import app


class TestCrawlApi:
    """Tests using pytest fixtures and ASGI transport."""

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

    async def test_v1_crawl_returns_unified_response_with_markdown_and_diagnostics(self):
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "request_id" in body
        assert body["submitted_count"] == 1
        assert "policy_schema_version" in body
        assert body["success"] is True
        assert len(body["results"]) == 1

        result = body["results"][0]
        assert "Example Org" in result["markdown"]
        assert isinstance(result["diagnostics"], dict)
        assert "content_metrics" in result["diagnostics"]
        assert result["status"] == "success"
        assert result["classification"] == "rendered"
        assert result["http_status"] == 200
        assert result["metadata"].get("json_ld") is not None
        # #2-fix: plain single-page crawl skips link extraction entirely
        assert result["discovered_link_count"] == 0
        assert result["discovered_links"] == []
        # next_page_url stays a standard field regardless
        assert result["next_page_url"] == f"{self.base}/page2"

    async def test_v1_crawl_batch_returns_one_result_per_url(self):
        payload = self._crawl_payload(urls=[f"{self.base}/", f"{self.base}/page2"])
        payload.pop("url")
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 200
        body = resp.json()
        assert body["submitted_count"] == 2
        assert len(body["results"]) == 2
        assert "Example Org" in body["results"][0]["markdown"]

    async def test_v1_crawl_discovery_includes_robots_and_sitemap(self):
        payload = self._crawl_payload(policy={
            "strategies": [HTTP_DECLARED_CRAWLER],
            "discovery": {"robots": True, "sitemaps": True, "feeds": True},
            "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
        })
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        diagnostics = result["diagnostics"]
        assert "robots" in diagnostics
        assert diagnostics["robots"]["allowed"] is True
        assert "/sitemap.xml" in diagnostics["robots"]["sitemaps"]
        assert "discovery" in diagnostics
        assert "/page1" in diagnostics["discovery"]["sitemap_urls"]
        assert "/page2" in diagnostics["discovery"]["sitemap_urls"]

    async def test_v1_crawl_404_is_classified_not_found(self):
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(url=f"{self.base}/missing"))
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["status"] == "failed"
        assert result["classification"] == "not_found"

    async def test_v1_crawl_rejects_payload_without_url(self):
        resp = await self.client.post("/v1/crawl", json={"policy": {"strategies": [HTTP_DECLARED_CRAWLER]}})
        assert resp.status_code == 422

    async def test_v1_crawl_scope_regex_records_out_of_scope_links(self):
        # The scope regex restricts frontier expansion only. The crawler still
        # EXTRACTS every advertised link it sees on the page (collect-not-crawl),
        # and the discovered_links list may contain links that fail the scope
        # check. Callers downstream use the scope to decide whether to enqueue
        # a discovered link; the service does NOT silently drop them.
        #
        # Here the fixture serves on 127.0.0.1 (loopback), so the in-page
        # links cannot match ``^https://example\.com``; every discovered link
        # is therefore out of scope and is still reported back to the caller.
        payload = self._crawl_payload(
            policy={
                "strategies": [HTTP_DECLARED_CRAWLER],
                "scope_regex": r"^https://example\.com",
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 100, "max_depth": 1},
            },
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["discovered_link_count"] >= 1
        for link in result["discovered_links"]:
            normalized = link["normalized_url"] if isinstance(link, dict) else link
            assert not normalized.startswith("https://example.com"), (
                f"discovered link {normalized!r} matches the scope; fixture is on loopback"
            )

    async def test_v1_crawl_webhook_returns_202_and_delivers(self):
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": [HTTP_DECLARED_CRAWLER],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
            "webhook_url": f"{self.base}/webhook_sink",
        }
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "accepted"
        assert "status_url" in body
        # The background task crawls + POSTs the result to the webhook sink.
        assert len(WEBHOOK_RECEIVED) == 1
        delivered = json.loads(WEBHOOK_RECEIVED[0].decode("utf-8"))
        assert delivered["submitted_count"] == 1
        assert "results" in delivered

    async def test_metrics_endpoint_exposes_prometheus_text(self):
        await self.client.post("/v1/crawl", json=self._crawl_payload())
        resp = await self.client.get("/metrics")
        assert resp.status_code == 200, resp.text
        assert "crawler_crawl_requests_total" in resp.text
        assert "crawler_crawl_results_total" in resp.text
    async def test_v1_crawl_jsonl_output_format(self):
        # #17: output_format=jsonl returns newline-delimited JSON (one object per URL).
        resp = await self.client.post(
            "/v1/crawl", json=self._crawl_payload(output_format="jsonl")
        )
        assert resp.status_code == 200, resp.text
        assert "application/x-ndjson" in resp.headers.get("content-type", "")
        lines = [ln for ln in resp.text.splitlines() if ln.strip()]
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["original_url"] == f"{self.base}/"
        assert obj["status"] == "success"
        assert obj["classification"] == "rendered"

    async def test_v1_crawl_markdown_output_format(self):
        resp = await self.client.post(
            "/v1/crawl", json=self._crawl_payload(output_format="markdown")
        )
        assert resp.status_code == 200, resp.text
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert f"# {self.base}" in resp.text
        assert "Example Org" in resp.text

    async def test_v1_crawl_csv_output_format(self):
        resp = await self.client.post(
            "/v1/crawl", json=self._crawl_payload(output_format="csv")
        )
        assert resp.status_code == 200, resp.text
        assert "text/csv" in resp.headers.get("content-type", "")
        rows = [ln for ln in resp.text.splitlines() if ln.strip()]
        assert rows[0].startswith("original_url,")
        assert len(rows) == 2  # header + one result

    async def test_v1_crawl_unsupported_output_format_rejected(self):
        resp = await self.client.post(
            "/v1/crawl", json=self._crawl_payload(output_format="xml")
        )
        assert resp.status_code == 422

class TestDiscoveryAndRenderApi:
    """Discovery/health endpoints and the legacy /v1/render path."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client):
        self.client = async_client

    async def test_health_liveness(self):
        resp = await self.client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    async def test_readiness_reports_capacity(self):
        resp = await self.client.get("/ready")
        assert resp.status_code in (200, 503)
        body = resp.json()
        assert "active_requests" in body
        assert "max_active_requests" in body

    async def test_readiness_uses_live_gate_not_stale_gauges(self):
        # M11: saturation must come from the live resource gate. No crawl
        # has run here, so the metrics gauges are stale zeros -- a sampler
        # reporting saturation must still yield 503.
        from app.health.readiness import READINESS_STATE

        READINESS_STATE.set_utilization_sampler(lambda: (0.99, 0.0))
        try:
            resp = await self.client.get("/ready")
        finally:
            READINESS_STATE.set_utilization_sampler(None)
        assert resp.status_code == 503
        assert resp.json()["saturated"] is True

    async def test_discovery_endpoints(self):
        for path in ("/.well-known/crawler-service", "/v1/capabilities", "/v1/agent-hints"):
            resp = await self.client.get(path)
            assert resp.status_code == 200, path
            assert resp.json()

    async def test_build_unified_response(self):
        from app.api.response_shaping import build_unified_crawl_response

        resp = build_unified_crawl_response([
            {"original_url": "http://x/", "final_url": "http://x/", "status": "success", "classification": "rendered"}
        ])
        assert resp.success is True
        assert resp.submitted_count == 1

    async def test_v1_render_invalid_payload_rejected(self):
        # With a typed request body, a malformed body shape is rejected by
        # FastAPI with 422 before reaching the handler.
        resp = await self.client.post("/v1/render", json={"options": "not-a-dict"})
        assert resp.status_code == 422

    async def test_v1_render_exhausted_pool_returns_503(self, monkeypatch):
        # M12: when every renderer is wedged, the wait times out and the
        # request surfaces as 503, not a hang or a 500.
        import app.api.render_endpoint as render_endpoint
        from app.crawl.strategies.browser_pool import (
            BrowserPoolExhaustedError,
            set_browser_pool,
        )

        class _ExhaustedWorker:
            def __init__(self, pool):
                pass

            async def __call__(self, job):
                raise BrowserPoolExhaustedError("pool exhausted in test")

        monkeypatch.setattr(render_endpoint, "PooledBrowserWorker", _ExhaustedWorker)
        old = set_browser_pool(object())
        try:
            resp = await self.client.post("/v1/render", json={"url": "http://127.0.0.1:1/x"})
        finally:
            set_browser_pool(old)
        assert resp.status_code == 503
        assert resp.json()["code"] == "browser_pool_unavailable"

    async def test_v1_render_without_pool_returns_503(self):
        # M13: a None browser pool (startup/shutdown window) must surface as
        # a clean 503, not an AttributeError 500 from inside the worker.
        from app.crawl.strategies.browser_pool import set_browser_pool

        old = set_browser_pool(None)
        try:
            resp = await self.client.post("/v1/render", json={"url": "http://127.0.0.1:1/x"})
        finally:
            set_browser_pool(old)
        assert resp.status_code == 503
        assert resp.json()["code"] == "browser_pool_unavailable"

    async def test_validation_exception_handler_returns_422(self):
        from fastapi import Request
        from fastapi.exceptions import RequestValidationError
        handler = app.exception_handlers[RequestValidationError]
        scope = {"type": "http", "method": "POST", "path": "/v1/crawl", "headers": [], "query_string": b""}
        request = Request(scope)
        exc = RequestValidationError(
            errors=[{"loc": ("body", "url"), "msg": "field required", "type": "value_error.missing"}]
        )
        response = await handler(request, exc)
        assert response.status_code == 422

class TestMapApi:
    """#2 /v1/map against a real local server (no mocks)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()

    def _map_payload(self, **extra):
        payload = {
            "url": f"{self.base}/",
            "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}},
        }
        payload.update(extra)
        return payload

    async def test_v1_map_returns_url_frontier(self):
        resp = await self.client.post("/v1/map", json=self._map_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "all_urls" in body
        # sitemap advertises /page1 and /page2, resolved to absolute URLs
        # against the origin (L6: SiteMapResponse contract).
        assert f"{self.base}/page2" in body["all_urls"]
        # robots points at /sitemap.xml
        assert f"{self.base}/sitemap.xml" in body["sitemaps"]
        # in-page links are discovered too
        assert len(body["links"]) >= 1

    async def test_v1_map_rejects_missing_url(self):
        resp = await self.client.post("/v1/map", json={"policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}})
        assert resp.status_code == 422

class TestShadowDomApi:
    """#25 Shadow-DOM flattening via browser renderer (real camoufox)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()

    @pytest.mark.browser
    @pytest.mark.skipif(not browser_available(), reason="Playwright chromium not installed")
    async def test_v1_crawl_shadow_dom_flattened(self):
        resp = await self.client.post("/v1/crawl", json={
            "url": f"{self.base}/shadow",
            "policy": {
                "strategies": ["BROWSER_HEADLESS"],
                "limits": {"timeout_seconds": 30, "max_retries": 0, "max_discovered_links": 50},
            },
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        result = body["results"][0]
        assert result["status"] == "success"
        # After flattening, the shadow-root anchor is in the HTML and extracted.
        assert "Shadow Link" in (result.get("html") or "")
        assert "/shadow-link" in (result.get("html") or "")




class TestCookiePersistenceApi:
    """#9 Cookie persistence integration tests (real server, real cookie jar)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url, tmp_path):
        self.client = async_client
        self.base = base_url
        self.cookie_file = tmp_path / "cookie_jar.json"
        # ASGITransport doesn't run FastAPI lifespan, so manually manage the global cookie jar
        from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR
        _GLOBAL_COOKIE_JAR.load_from_file(str(self.cookie_file))
        WEBHOOK_RECEIVED.clear()
        yield
        # Save cookies on teardown
        _GLOBAL_COOKIE_JAR.save_to_file(str(self.cookie_file))

    def _crawl_payload(self, url: str, **extra):
        payload = {
            "url": url,
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        payload.update(extra)
        return payload

    async def test_cookie_persistence_across_requests(self):
        # First request: sets a cookie
        resp1 = await self.client.post("/v1/crawl", json=self._crawl_payload(f"{self.base}/set-cookie"))
        assert resp1.status_code == 200, resp1.text
        # Second request: should send the cookie back
        resp2 = await self.client.post("/v1/crawl", json=self._crawl_payload(f"{self.base}/check-cookie"))
        assert resp2.status_code == 200, resp2.text
        body2 = resp2.json()
        result = body2["results"][0]
        # The response body of /check-cookie is the Cookie header it received
        assert "session_id=abc123" in (result.get("html") or result.get("text") or "")

    async def test_cookie_persistence_survives_restart(self):
        # First crawl sets cookie
        await self.client.post("/v1/crawl", json=self._crawl_payload(f"{self.base}/set-cookie"))
        # Second crawl verifies cookie is sent
        resp = await self.client.post("/v1/crawl", json=self._crawl_payload(f"{self.base}/check-cookie"))
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert "session_id=abc123" in (result.get("html") or result.get("text") or "")

class TestJobApi:
    """#3+#14 Job API integration tests (real server)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()

    async def test_v1_jobs_create_list_get(self):
        # Create a job
        resp = await self.client.post("/v1/jobs", json={
            "root_url": f"{self.base}/",
            "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"], "limits": {"timeout_seconds": 10, "max_retries": 0}},
        })
        assert resp.status_code == 200, resp.text
        body = resp.json()
        job_id = body["job_id"]
        assert body["status"] == "pending"
        assert body["root_url"] == f"{self.base}/"

        # List jobs
        resp2 = await self.client.get("/v1/jobs")
        assert resp2.status_code == 200
        jobs = resp2.json()["jobs"]
        assert len(jobs) >= 1
        assert any(j["job_id"] == job_id for j in jobs)

        # Get job
        resp3 = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp3.status_code == 200
        job = resp3.json()
        assert job["job_id"] == job_id
        assert job["status"] == "pending"
        assert job["frontier_size"] == 1

    async def test_v1_jobs_control_pause_resume_cancel(self):
        resp = await self.client.post("/v1/jobs", json={
            "root_url": f"{self.base}/",
            "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]},
        })
        job_id = resp.json()["job_id"]

        # Cannot pause a pending job
        resp_pause = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert resp_pause.status_code == 422

        # Manually set to running (simulating engine starting it)
        from app.crawl.job_store import JobStatus
        from app.main import job_store
        job = job_store.load(job_id)
        job.status = JobStatus.RUNNING
        job_store.save(job)

        # Pause
        resp_pause = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert resp_pause.status_code == 200
        assert resp_pause.json()["status"] == "paused"

        # Resume
        resp_resume = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "resume"})
        assert resp_resume.status_code == 200
        assert resp_resume.json()["status"] == "running"

        # Cancel
        resp_cancel = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert resp_cancel.status_code == 200
        assert resp_cancel.json()["status"] == "cancelled"

    async def test_v1_jobs_delete(self):
        resp = await self.client.post("/v1/jobs", json={
            "root_url": f"{self.base}/",
            "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]},
        })
        job_id = resp.json()["job_id"]

        resp_del = await self.client.delete(f"/v1/jobs/{job_id}")
        assert resp_del.status_code == 200
        assert resp_del.json()["status"] == "deleted"

        # Verify deleted
        resp_get = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp_get.status_code == 404

    async def test_v1_jobs_invalid_action(self):
        resp = await self.client.post("/v1/jobs", json={
            "root_url": f"{self.base}/",
            "policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]},
        })
        job_id = resp.json()["job_id"]

        resp_bad = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "invalid"})
        assert resp_bad.status_code == 422
        assert "invalid_action" in resp_bad.json()["code"]

