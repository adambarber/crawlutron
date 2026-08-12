import asyncio
import logging
import os
import tempfile
import sys
import unittest
from pathlib import Path

CRAWLER_APP = Path(__file__).resolve().parents[2]
SHARED_PYTHON = Path(__file__).resolve().parents[4] / "docker" / "shared" / "python"
os.environ.setdefault("BROWSER_CRAWLER_LOG_FILE", str(Path(tempfile.gettempdir()) / "browser-crawler-test.jsonl"))
sys.path.insert(0, str(SHARED_PYTHON))
sys.path.insert(0, str(CRAWLER_APP))

from fastapi.testclient import TestClient

from app import main as crawler_main
from app.models import CrawlJob, CrawlResult
from app.service import render_request
from app.settings import CrawlerSettings


class BrowserCrawlerApiTest(unittest.TestCase):
    def test_main_configures_shared_json_logging_from_settings(self) -> None:
        self.assertEqual(crawler_main.logger.name, "browser-crawler")
        self.assertEqual(logging.getLevelName(crawler_main.logger.level), crawler_main.settings.log_level.upper())
        self.assertEqual(crawler_main.settings.log_file_path, os.environ["BROWSER_CRAWLER_LOG_FILE"])
        self.assertEqual(crawler_main.settings.log_file_max_bytes, 10 * 1024 * 1024)
        self.assertEqual(crawler_main.settings.log_file_backup_count, 3)

    def test_capabilities_endpoint_documents_machine_readable_contract(self) -> None:
        client = TestClient(crawler_main.app)

        response = client.get("/v1/capabilities")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["service"], "browser-crawler")
        self.assertEqual(body["policy_schema_version"], "2026-06-23")
        self.assertIn("HTTP_DECLARED_CRAWLER", body["supported_strategies"])
        self.assertIn("HTTP_CFFI_IMPERSONATE", body["supported_strategies"])
        self.assertIn("TRAFILATURA_EXTRACT", body["supported_strategies"])
        self.assertTrue(body["features"]["cloudflare_bypass"])
        self.assertIn("captcha_detected", body["supported_classifications"])
        self.assertIn("selected_strategy", body["output_fields"])
        self.assertIn("discovered_links", body["output_fields"])
        self.assertIn("classification_evidence", body["output_fields"])
        self.assertIn("max_discovered_links", body["default_limits"])
        self.assertIn("diagnostics", body["output_fields"])
        self.assertEqual(body["default_limits"]["max_batch_size"], crawler_main.settings.max_batch_size)
        self.assertTrue(body["features"]["direct_http"])
        self.assertTrue(body["features"]["headed_browser"])
        self.assertTrue(body["features"]["frame_discovery"])
        self.assertTrue(body["features"]["form_discovery"])
        self.assertTrue(body["features"]["readiness_endpoint"])
        self.assertTrue(body["features"]["event_loop_thread_offload"])
        self.assertFalse(body["features"]["captcha_solving"])
        self.assertFalse(body["features"]["proxy_rotation"])
        self.assertFalse(body["features"]["identity_rotation"])
        self.assertFalse(body["features"]["credential_bypass"])
        self.assertTrue(body["features"]["cloudflare_bypass"])
        self.assertEqual(body["openapi_url"], "/openapi.json")
        self.assertEqual(body["swagger_ui_url"], "/docs")

    def test_agent_hints_endpoint_explains_strategy_selection_and_interpretation(self) -> None:
        client = TestClient(crawler_main.app)

        response = client.get("/v1/agent-hints")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        guidance_text = " ".join(body["guidance"]).lower()
        self.assertIn("prefer direct transparent crawler http", guidance_text)
        self.assertIn("do not treat http 200 as content success", guidance_text)
        self.assertIn("frames", guidance_text)
        self.assertIn("forms", guidance_text)
        self.assertIn("pagination", guidance_text)
        self.assertIn("do not bypass cloudflare", guidance_text)
        self.assertIn("do not solve captchas", guidance_text)
        self.assertIn("do not rotate proxies", guidance_text)
        self.assertIn("get /health", guidance_text)
        self.assertIn("get /ready", guidance_text)
        self.assertIn("saturated", guidance_text)
        self.assertIn("backpressure", guidance_text)
        self.assertIn("fastapi event loop", guidance_text)
        self.assertIn("offloaded", guidance_text)
        self.assertIn("HTTP_SITE_COMPAT", body["strategies"])
        self.assertIn("browser_fallback", body["example_policies"])
        self.assertIn("DOCUMENT_EXTRACT", body["example_policies"]["document_extraction"]["strategies"])
        self.assertIn("html-only", body["strategies"]["TRAFILATURA_EXTRACT"].lower())
        self.assertIn("does not parse pdf", body["strategies"]["TRAFILATURA_EXTRACT"].lower())

    def test_health_endpoint_remains_lightweight_liveness(self) -> None:
        client = TestClient(crawler_main.app)

        response = client.get("/health")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"status": "ok"})

    def test_readiness_endpoint_reports_crawl_capacity(self) -> None:
        client = TestClient(crawler_main.app)
        crawler_main.readiness_state.begin_request()
        try:
            response = client.get("/ready")
        finally:
            crawler_main.readiness_state.end_request()

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["status"], "ready")
        self.assertEqual(body["active_requests"], 1)
        self.assertEqual(body["max_active_requests"], crawler_main.settings.global_concurrency)
        self.assertFalse(body["saturated"])

    def test_readiness_endpoint_reports_saturation_without_affecting_liveness(self) -> None:
        client = TestClient(crawler_main.app)
        tokens = [crawler_main.readiness_state.begin_request() for _ in range(crawler_main.settings.global_concurrency)]
        try:
            readiness = client.get("/ready")
            health = client.get("/health")
        finally:
            for _token in tokens:
                crawler_main.readiness_state.end_request()

        self.assertEqual(readiness.status_code, 503)
        self.assertEqual(readiness.json()["status"], "saturated")
        self.assertTrue(readiness.json()["saturated"])
        self.assertEqual(health.status_code, 200)
        self.assertEqual(health.json(), {"status": "ok"})

    def test_well_known_discovery_document_points_agents_to_contracts(self) -> None:
        client = TestClient(crawler_main.app)

        response = client.get("/.well-known/crawler-service")

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["service"], "browser-crawler")
        self.assertEqual(body["version"], crawler_main.API_VERSION)
        self.assertEqual(body["links"]["openapi"], "/openapi.json")
        self.assertEqual(body["links"]["liveness"], "/health")
        self.assertEqual(body["links"]["readiness"], "/ready")
        self.assertEqual(body["links"]["capabilities"], "/v1/capabilities")
        self.assertEqual(body["links"]["agent_hints"], "/v1/agent-hints")
        self.assertIn("GET /health", body["recommended_endpoints"])
        self.assertIn("GET /ready", body["recommended_endpoints"])
        self.assertIn("POST /v1/crawl", body["recommended_endpoints"])

    def test_openapi_schema_documents_crawl_endpoint_enums_and_examples(self) -> None:
        client = TestClient(crawler_main.app)

        schema = client.get("/openapi.json").json()

        self.assertEqual(schema["info"]["title"], "Browser Crawler Execution Service")
        tag_descriptions = {tag["name"]: tag["description"] for tag in schema["tags"]}
        self.assertIn("Crawler", tag_descriptions)
        self.assertIn("Health", tag_descriptions)
        self.assertIn("crawl-capacity readiness", tag_descriptions["Health"])
        self.assertIn("/health", schema["paths"])
        self.assertIn("/ready", schema["paths"])
        health_get = schema["paths"]["/health"]["get"]
        ready_get = schema["paths"]["/ready"]["get"]
        self.assertEqual(health_get["summary"], "Service liveness check")
        self.assertIn("Docker healthchecks", health_get["description"])
        self.assertEqual(ready_get["summary"], "Service readiness check")
        self.assertIn("backpressure", ready_get["description"])
        self.assertIn("503", ready_get["responses"])
        self.assertIn("ReadinessResponse", schema["components"]["schemas"])
        readiness_schema = schema["components"]["schemas"]["ReadinessResponse"]
        self.assertIn("active_requests", readiness_schema["properties"])
        self.assertIn("saturated", readiness_schema["properties"])
        self.assertIn("/v1/crawl", schema["paths"])
        crawl_post = schema["paths"]["/v1/crawl"]["post"]
        self.assertEqual(crawl_post["summary"], "Execute a policy-driven crawl")
        self.assertIn("policy is the caller-controlled execution plan", crawl_post["description"].lower())
        self.assertIn("strategies run in order", crawl_post["description"].lower())
        self.assertIn("request_profile", crawl_post["description"])
        self.assertIn("browser_profile", crawl_post["description"])
        self.assertIn("discovery", crawl_post["description"])
        self.assertIn("diagnostics.frames", crawl_post["description"])
        self.assertIn("diagnostics.forms", crawl_post["description"])
        self.assertIn("pagination", crawl_post["description"].lower())
        self.assertIn("examples", crawl_post["requestBody"]["content"]["application/json"])
        strategy_schema = schema["components"]["schemas"]["CrawlStrategy"]
        self.assertIn("HTTP_DECLARED_CRAWLER", strategy_schema["enum"])
        self.assertIn("BROWSER_HEADED_WARMED", strategy_schema["enum"])
        self.assertIn("TRAFILATURA_EXTRACT", strategy_schema["enum"])
        classification_schema = schema["components"]["schemas"]["CrawlClassification"]
        self.assertIn("captcha_detected", classification_schema["enum"])
        self.assertIn("rendered", classification_schema["enum"])
        self.assertIn("CrawlerError", schema["components"]["schemas"])
        self.assertIn("CondensedCrawlResponse", schema["components"]["schemas"])
        condensed_schema = schema["components"]["schemas"]["CondensedCrawlResponse"]
        self.assertIn("success", condensed_schema["properties"])
        self.assertIn("data", condensed_schema["properties"])
        self.assertIn("CondensedCrawlResult", schema["components"]["schemas"])
        self.assertIn("FirecrawlMetadata", schema["components"]["schemas"])
        policy_schema = schema["components"]["schemas"]["CrawlPolicy"]
        limits_schema = schema["components"]["schemas"]["CrawlPolicyLimits"]
        self.assertIn("max_discovered_links", limits_schema["properties"])
        self.assertIn("request_profile", policy_schema["properties"])
        self.assertIn("browser_profile", policy_schema["properties"])
        self.assertIn("discovery", policy_schema["properties"])
        self.assertIn("user_agent_family", policy_schema["properties"]["request_profile"]["description"])
        self.assertIn("viewport", policy_schema["properties"]["browser_profile"]["description"])
        self.assertIn("robots", policy_schema["properties"]["discovery"]["description"])
        self.assertIn("default", policy_schema["properties"]["strategies"]["description"].lower())
        self.assertIn("bounded", limits_schema["properties"]["max_discovered_links"]["description"].lower())

    def test_crawl_endpoint_returns_per_url_provenance_and_attempt_history(self) -> None:
        client = TestClient(crawler_main.app)

        async def fake_crawl_request(payload, *, settings=None, browser_worker=None, http_fetcher=None):
            return {
                "request_id": "crawl_contract",
                "submitted_count": 1,
                "policy_schema_version": "2026-06-23",
                "results": [
                    {
                        "original_url": "https://records.example.gov/search?q=permit",
                        "normalized_url": "https://records.example.gov/search?q=permit",
                        "final_url": "https://records.example.gov/search?q=permit",
                        "status": "success",
                        "classification": "rendered",
                        "selected_strategy": "HTTP_DECLARED_CRAWLER",
                        "attempted_strategies": [
                            {
                                "strategy": "HTTP_DECLARED_CRAWLER",
                                "status": "success",
                                "classification": "rendered",
                                "http_status": 200,
                                "content_type": "text/html; charset=utf-8",
                                "elapsed_ms": 4,
                                "error": None,
                            }
                        ],
                        "http_status": 200,
                        "headers": {"content-type": "text/html; charset=utf-8"},
                        "content_type": "text/html; charset=utf-8",
                        "html": "<html><body><main>Permit records</main><a href=\"/permits\">Permits</a></body></html>",
                        "text": "Permit records Permits",
                        "extraction_method": "http_html",
                        "quality_score": 0.95,
                        "classification_evidence": [],
                        "diagnostics": {
                            "request_profile": {"user_agent_family": "desktop_chrome", "cookie_jar": "none"},
                            "content_metrics": {"visible_text_length": 25, "html_length": 85, "link_count": 1},
                            "discovery": {"sitemap_urls": [], "feeds": []},
                        },
                        "discovered_link_count": 1,
                        "discovered_links_truncated": False,
                        "discovered_links": [
                            {
                                "url": "/permits",
                                "normalized_url": "https://records.example.gov/permits",
                                "text": "Permits",
                                "rel": [],
                                "source_url": "https://records.example.gov/search?q=permit",
                                "host": "records.example.gov",
                                "is_same_host": True,
                            }
                        ],
                        "error": None,
                    }
                ],
            }

        import unittest.mock
        with unittest.mock.patch("app.main.crawl_request", side_effect=fake_crawl_request):
            response = client.post(
                "/v1/crawl",
                json={
                    "url": "https://records.example.gov/search?q=permit",
                    "policy": {
                        "strategies": ["HTTP_DECLARED_CRAWLER"],
                        "request_profile": {"user_agent_family": "desktop_chrome"},
                    },
                },
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["success"])
        self.assertEqual(len(body["data"]), 1)
        result = body["data"][0]
        self.assertEqual(result["url"], "https://records.example.gov/search?q=permit")
        self.assertIsNotNone(result["markdown"])
        self.assertIn("Permit records", result["markdown"])
        self.assertEqual(result["metadata"].get("source_url"), "https://records.example.gov/search?q=permit")

    def test_crawl_endpoint_returns_stable_validation_error_schema(self) -> None:
        client = TestClient(crawler_main.app)

        response = client.post(
            "/v1/crawl",
            json={
                "url": "https://records.example.gov/search",
                "policy": {"strategies": ["UNKNOWN_STRATEGY"]},
            },
        )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["code"], "validation_error")
        self.assertEqual(body["message"], "Request validation failed")
        self.assertIn("errors", body["details"])

    def test_render_request_accepts_single_url_payload_and_returns_response_shape(self) -> None:
        async def worker(job: CrawlJob) -> CrawlResult:
            return CrawlResult(
                url=job.url,
                status="success",
                classification="rendered",
                final_url=job.url,
                html="<html><body>Rendered</body></html>",
                text="Rendered",
                attempts=1,
                elapsed_ms=3,
                domain=job.domain,
                metadata={"title": "Rendered"},
            )

        response = asyncio.run(
            render_request(
                {"url": "https://records.example.gov/search"},
                settings=CrawlerSettings(max_batch_size=10, per_domain_delay_ms=0),
                worker=worker,
            )
        )

        self.assertTrue(response["request_id"].startswith("crawl_"))
        self.assertEqual(response["submitted_count"], 1)
        self.assertEqual(response["results"][0]["status"], "success")
        self.assertEqual(response["results"][0]["classification"], "rendered")
        self.assertEqual(response["results"][0]["html"], "<html><body>Rendered</body></html>")
        self.assertEqual(response["results"][0]["text"], "Rendered")
        self.assertEqual(response["results"][0]["domain"], "example.gov")

    def test_render_request_preserves_batch_order_when_worker_completes_out_of_order(self) -> None:
        release = asyncio.Event()

        async def worker(job: CrawlJob) -> CrawlResult:
            if job.index == 0:
                await release.wait()
            else:
                release.set()
            return CrawlResult(
                url=job.url,
                status="success",
                classification="rendered",
                final_url=job.url,
                html=f"<html>{job.index}</html>",
                text=str(job.index),
                attempts=1,
                elapsed_ms=1,
                domain=job.domain,
            )

        response = asyncio.run(
            render_request(
                {"urls": ["https://a.example.gov/slow", "https://b.example.org/fast"]},
                settings=CrawlerSettings(global_concurrency=2, per_domain_delay_ms=0),
                worker=worker,
            )
        )

        self.assertEqual([result["url"] for result in response["results"]], ["https://a.example.gov/slow", "https://b.example.org/fast"])
        self.assertEqual([result["text"] for result in response["results"]], ["0", "1"])

    def test_render_request_returns_per_url_disallowed_result_for_invalid_urls(self) -> None:
        async def worker(job: CrawlJob) -> CrawlResult:
            raise AssertionError("invalid URLs must not reach the browser worker")

        response = asyncio.run(
            render_request(
                {"urls": ["file:///etc/passwd", "https://records.example.gov/public"]},
                settings=CrawlerSettings(per_domain_delay_ms=0),
                worker=lambda job: CrawlResult(
                    url=job.url,
                    status="success",
                    classification="rendered",
                    final_url=job.url,
                    html="<html></html>",
                    text="ok",
                    attempts=1,
                    elapsed_ms=1,
                    domain=job.domain,
                ),
            )
        )

        self.assertEqual(response["results"][0]["status"], "disallowed")
        self.assertEqual(response["results"][0]["classification"], "unsupported_scheme")
        self.assertEqual(response["results"][1]["status"], "success")

    def test_render_request_rejects_oversized_batches(self) -> None:
        with self.assertRaises(ValueError) as context:
            asyncio.run(
                render_request(
                    {"urls": ["https://one.example.gov", "https://two.example.gov"]},
                    settings=CrawlerSettings(max_batch_size=1),
                    worker=lambda job: None,
                )
            )

        self.assertEqual(str(context.exception), "batch_size_exceeded")


if __name__ == "__main__":
    unittest.main()
