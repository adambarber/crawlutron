"""End-to-end /v1/crawl behavior against a real local HTTP server (no mocks)."""

import json

import pytest
import httpx
from app.main import app, build_condensed_crawl_response
from helpers.server import WEBHOOK_RECEIVED
from app.constants import HTTP_DECLARED_CRAWLER


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
        assert result["discovered_link_count"] >= 1
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

    async def test_v1_crawl_scope_regex_limits_discovered_links(self):
        payload = self._crawl_payload(
            policy={
                "strategies": [HTTP_DECLARED_CRAWLER],
                "scope_regex": r"^https://example\.com",
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 100},
            },
        )
        resp = await self.client.post("/v1/crawl", json=payload)
        assert resp.status_code == 200
        result = resp.json()["results"][0]
        assert result["discovered_link_count"] == 0
        assert result["discovered_links"] == []

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

    async def test_discovery_endpoints(self):
        for path in ("/.well-known/crawler-service", "/v1/capabilities", "/v1/agent-hints"):
            resp = await self.client.get(path)
            assert resp.status_code == 200, path
            assert resp.json()

    async def test_condensed_response_alias(self):
        resp = build_condensed_crawl_response([
            {"original_url": "http://x/", "final_url": "http://x/", "status": "success", "classification": "rendered"}
        ])
        assert resp.success is True
        assert resp.submitted_count == 1

    async def test_v1_render_invalid_payload_rejected(self):
        resp = await self.client.post("/v1/render", json={"options": "not-a-dict"})
        assert resp.status_code == 400

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
