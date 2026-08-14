"""End-to-end /v1/crawl behavior against a real local HTTP server (no mocks)."""

import asyncio
import json
import unittest

from fastapi.testclient import TestClient

from app.main import app
from helpers.server import spawn_server, WEBHOOK_RECEIVED


class CrawlApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = spawn_server()
        cls.base = cls.server.base_url

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.stop()

    def setUp(self) -> None:
        self.client = TestClient(app)
        WEBHOOK_RECEIVED.clear()

    def test_v1_crawl_returns_unified_response_with_markdown_and_diagnostics(self) -> None:
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 200, resp.text)
        body = resp.json()
        # Unified envelope shape
        self.assertIn("request_id", body)
        self.assertEqual(body["submitted_count"], 1)
        self.assertIn("policy_schema_version", body)
        self.assertTrue(body["success"])
        self.assertEqual(len(body["results"]), 1)

        result = body["results"][0]
        # Both LLM markdown and full diagnostics are present.
        self.assertIn("Example Org", result["markdown"])
        self.assertIsInstance(result["diagnostics"], dict)
        self.assertIn("content_metrics", result["diagnostics"])
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["classification"], "rendered")
        self.assertEqual(result["http_status"], 200)
        # Firecrawl metadata + JSON-LD
        self.assertIsNotNone(result["metadata"].get("json_ld"))
        # Discovery + pagination captured
        self.assertGreaterEqual(result["discovered_link_count"], 1)
        self.assertEqual(result["next_page_url"], f"{self.base}/page2")

    def test_v1_crawl_batch_returns_one_result_per_url(self) -> None:
        payload = {
            "urls": [f"{self.base}/", f"{self.base}/page2"],
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["submitted_count"], 2)
        self.assertEqual(len(body["results"]), 2)
        self.assertIn("Example Org", body["results"][0]["markdown"])

    def test_v1_crawl_discovery_includes_robots_and_sitemap(self) -> None:
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "discovery": {"robots": True, "sitemaps": True, "feeds": True},
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        diagnostics = result["diagnostics"]
        self.assertIn("robots", diagnostics)
        self.assertTrue(diagnostics["robots"]["allowed"])
        self.assertIn("/sitemap.xml", diagnostics["robots"]["sitemaps"])
        self.assertIn("discovery", diagnostics)
        self.assertIn("/page1", diagnostics["discovery"]["sitemap_urls"])
        self.assertIn("/page2", diagnostics["discovery"]["sitemap_urls"])

    def test_v1_crawl_404_is_classified_not_found(self) -> None:
        payload = {
            "url": f"{self.base}/missing",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "not_found")

    def test_v1_crawl_rejects_payload_without_url(self) -> None:
        resp = self.client.post("/v1/crawl", json={"policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}})
        self.assertEqual(resp.status_code, 422)

    def test_v1_crawl_scope_regex_limits_discovered_links(self) -> None:
        # The server runs on http://127.0.0.1; a scope regex that only matches
        # https://example.com must filter out every discovered loopback link.
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "scope_regex": r"^https://example\.com",
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 100},
            },
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 200)
        result = resp.json()["results"][0]
        self.assertEqual(result["discovered_link_count"], 0)
        self.assertEqual(result["discovered_links"], [])


    def test_v1_crawl_webhook_returns_202_and_delivers(self) -> None:
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
            "webhook_url": f"{self.base}/webhook_sink",
        }
        resp = self.client.post("/v1/crawl", json=payload)
        self.assertEqual(resp.status_code, 202, resp.text)
        body = resp.json()
        self.assertEqual(body["status"], "accepted")
        self.assertIn("status_url", body)
        # The background task crawls + POSTs the result to the webhook sink.
        self.assertEqual(len(WEBHOOK_RECEIVED), 1)
        delivered = json.loads(WEBHOOK_RECEIVED[0].decode("utf-8"))
        self.assertEqual(delivered["submitted_count"], 1)
        self.assertIn("results", delivered)

    def test_metrics_endpoint_exposes_prometheus_text(self) -> None:
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
        }
        self.client.post("/v1/crawl", json=payload)
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200, resp.text)
        self.assertIn("crawler_crawl_requests_total", resp.text)
        self.assertIn("crawler_crawl_results_total", resp.text)
        # After one crawl, at least one result was observed.
        self.assertIn("crawler_crawl_results_total 1", resp.text)


if __name__ == "__main__":
    unittest.main()



class DiscoveryAndRenderApiTest(unittest.TestCase):
    """Discovery/health endpoints and the legacy /v1/render path (real browser)."""

    def setUp(self) -> None:
        self.client = TestClient(app)

    def test_health_liveness(self) -> None:
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["status"], "ok")

    def test_readiness_reports_capacity(self) -> None:
        resp = self.client.get("/ready")
        self.assertIn(resp.status_code, (200, 503))
        body = resp.json()
        self.assertIn("active_requests", body)
        self.assertIn("max_active_requests", body)

    def test_discovery_endpoints(self) -> None:
        for path in ("/.well-known/crawler-service", "/v1/capabilities", "/v1/agent-hints"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, path)
            self.assertTrue(resp.json())

    def test_condensed_response_alias(self) -> None:
        from app.main import build_condensed_crawl_response
        resp = build_condensed_crawl_response([
            {"original_url": "http://x/", "final_url": "http://x/", "status": "success", "classification": "rendered"}
        ])
        self.assertTrue(resp.success)
        self.assertEqual(resp.submitted_count, 1)

    def test_v1_render_uses_real_browser(self) -> None:
        server = spawn_server()
        try:
            payload = {"url": f"{server.base_url}/", "options": {"include_html": True, "include_text": True}}
            resp = self.client.post("/v1/render", json=payload)
            self.assertEqual(resp.status_code, 200, resp.text)
            body = resp.json()
            self.assertEqual(body["submitted_count"], 1)
            result = body["results"][0]
            self.assertEqual(result["status"], "success")
            self.assertIn("Example Org", result["text"] or result["html"] or "")
        finally:
            server.stop()

    def test_v1_render_invalid_payload_rejected(self) -> None:
        resp = self.client.post("/v1/render", json={"options": "not-a-dict"})
        self.assertEqual(resp.status_code, 400)

    def test_validation_exception_handler_returns_422(self) -> None:
        from fastapi import Request
        from fastapi.exceptions import RequestValidationError
        handler = app.exception_handlers[RequestValidationError]
        scope = {"type": "http", "method": "POST", "path": "/v1/crawl", "headers": [], "query_string": b""}
        request = Request(scope)
        exc = RequestValidationError(
            errors=[{"loc": ("body", "url"), "msg": "field required", "type": "value_error.missing"}]
        )
        import asyncio
        response = asyncio.run(handler(request, exc))
        self.assertEqual(response.status_code, 422)
