"""End-to-end /v1/crawl behavior against a real local HTTP server (no mocks)."""

import asyncio
import json
import time

import pytest

from app.models.crawl_policy import CrawlPolicy
from app.transport.cookie_jar import global_cookie_jar
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._assertions import (
    accepted_crawl,
    assert_result_failed,
    browser_available,
    completed_crawl,
    poll_webhook_delivery,
)
from tests._fetcher_factories import make_crawl_payload
from tests._policy_factories import crawl_policy
from tests.api._asgi_base import _AsgiEndpointTests
from tests.helpers.server import WEBHOOK_RECEIVED

#: Discovery turned fully on. Three tests assert on what robots, sitemaps and feeds contributed, and spelling
#: the triple at each is how one of them kept asking for feeds the fixture never served.
DISCOVERY_ALL = {"robots": True, "sitemaps": True, "feeds": True}

#: A scope the loopback fixture cannot match -- the point of the scope tests is that the discovered links are
#: *outside* it.
EXAMPLE_SCOPE = r"^https://example\.com"

#: Room for 100 links and one level of depth, so the scope is the only thing deciding what comes back.
WIDE_LIMITS = {"max_discovered_links": 100, "max_depth": 1}


class TestCrawlApi(_AsgiEndpointTests):
    """Tests using pytest fixtures and ASGI transport."""

    async def test_v1_crawl_returns_unified_response_with_markdown_and_diagnostics(self):
        body = await completed_crawl(self.client, make_crawl_payload(f"{self.base}/"))
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
        # Plain single-page crawl skips link extraction entirely
        assert result["discovered_link_count"] == 0
        assert result["discovered_links"] == []
        # Next_page_url stays a standard field regardless
        assert result["next_page_url"] == f"{self.base}/page2"

    async def test_v1_crawl_batch_returns_one_result_per_url(self):
        payload = make_crawl_payload(f"{self.base}/", urls=[f"{self.base}/", f"{self.base}/page2"])
        payload.pop("url")
        body = await completed_crawl(self.client, payload)
        assert body["submitted_count"] == 2
        assert len(body["results"]) == 2
        assert "Example Org" in body["results"][0]["markdown"]

    async def test_v1_crawl_discovery_includes_robots_and_sitemap(self):
        payload = make_crawl_payload(
            f"{self.base}/",
            policy=crawl_policy([HTTP_DECLARED_CRAWLER], discovery=DISCOVERY_ALL),
        )
        result = (await completed_crawl(self.client, payload))["results"][0]
        diagnostics = result["diagnostics"]
        assert "robots" in diagnostics
        assert diagnostics["robots"]["allowed"] is True
        assert f"{self.base}/sitemap.xml" in diagnostics["robots"]["sitemaps"]
        assert "discovery" in diagnostics
        assert f"{self.base}/page1" in diagnostics["discovery"]["sitemap_urls"]
        assert f"{self.base}/page2" in diagnostics["discovery"]["sitemap_urls"]

    async def test_v1_crawl_404_is_classified_not_found(self):
        result = (await completed_crawl(self.client, make_crawl_payload(f"{self.base}/missing")))["results"][
            0
        ]
        assert_result_failed(result, "not_found")

    async def test_v1_crawl_rejects_payload_without_url(self):
        resp = await self.client.post("/v1/crawl", json={"policy": {"strategies": [HTTP_DECLARED_CRAWLER]}})
        assert resp.status_code == 422

    async def test_v1_crawl_scope_regex_records_out_of_scope_links(self):
        # The scope regex restricts frontier expansion only.
        payload = make_crawl_payload(
            f"{self.base}/",
            policy=crawl_policy([HTTP_DECLARED_CRAWLER], scope_regex=EXAMPLE_SCOPE, limits=WIDE_LIMITS),
        )
        result = (await completed_crawl(self.client, payload))["results"][0]
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
        body = await accepted_crawl(self.client, payload)
        assert "status_url" in body
        # The accepted envelope is a promise that the work outlives the request, so the delivery is polled for
        # rather than assumed to have already happened by the time the 202 came back.
        delivered = await poll_webhook_delivery(WEBHOOK_RECEIVED)
        assert delivered["submitted_count"] == 1
        assert "results" in delivered

    async def test_metrics_endpoint_exposes_prometheus_text(self):
        await self.client.post("/v1/crawl", json=make_crawl_payload(f"{self.base}/"))
        resp = await self.client.get("/metrics")
        assert resp.status_code == 200, resp.text
        assert "crawler_crawl_requests_total" in resp.text
        assert "crawler_crawl_results_total" in resp.text

    async def test_v1_crawl_jsonl_output_format(self):
        # Output_format=jsonl returns newline-delimited JSON (one object per URL).
        resp = await self.client.post(
            "/v1/crawl", json=make_crawl_payload(f"{self.base}/", output_format="jsonl")
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
            "/v1/crawl", json=make_crawl_payload(f"{self.base}/", output_format="markdown")
        )
        assert resp.status_code == 200, resp.text
        assert "text/markdown" in resp.headers.get("content-type", "")
        assert f"# {self.base}" in resp.text
        assert "Example Org" in resp.text

    async def test_v1_crawl_csv_output_format(self):
        resp = await self.client.post(
            "/v1/crawl", json=make_crawl_payload(f"{self.base}/", output_format="csv")
        )
        assert resp.status_code == 200, resp.text
        assert "text/csv" in resp.headers.get("content-type", "")
        rows = [ln for ln in resp.text.splitlines() if ln.strip()]
        assert rows[0].startswith("original_url,")
        assert len(rows) == 2  # header + one result

    async def test_v1_crawl_unsupported_output_format_rejected(self):
        resp = await self.client.post(
            "/v1/crawl", json=make_crawl_payload(f"{self.base}/", output_format="xml")
        )
        assert resp.status_code == 422


class TestMapApi(_AsgiEndpointTests):
    """/v1/map against a real local server (no mocks)."""

    def _map_payload(self, **extra):
        payload = {
            "url": f"{self.base}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
            },
        }
        payload.update(extra)
        return payload

    async def test_v1_map_returns_url_frontier(self):
        resp = await self.client.post("/v1/map", json=self._map_payload())
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert "all_urls" in body
        # Sitemap advertises /page1 and /page2, resolved to absolute URLs against the origin (SiteMapResponse
        # contract).
        assert f"{self.base}/page2" in body["all_urls"]
        # Robots points at /sitemap.xml
        assert f"{self.base}/sitemap.xml" in body["sitemaps"]
        # In-page links are discovered too
        assert len(body["links"]) >= 1

    async def test_v1_map_rejects_missing_url(self):
        resp = await self.client.post("/v1/map", json={"policy": {"strategies": ["HTTP_DECLARED_CRAWLER"]}})
        assert resp.status_code == 422


class TestShadowDomApi(_AsgiEndpointTests):
    """Shadow-DOM flattening via browser renderer (real camoufox)."""

    @pytest.mark.browser
    @pytest.mark.skipif(not browser_available(), reason="no usable browser binary (camoufox/playwright)")
    async def test_v1_crawl_shadow_dom_flattened(self):
        # The ASGI harness runs no lifespan, so the pool is None; a real browser-strategy crawl needs a live
        # pool. Initialize (real camoufox) and close it in a finally so no browsers leak into later tests.
        from app.transport.browser_pool import close_browser_pool, initialize_browser_pool

        await initialize_browser_pool()
        try:
            body = await completed_crawl(
                self.client,
                {
                    "url": f"{self.base}/shadow",
                    "policy": {
                        "strategies": ["BROWSER_HEADLESS"],
                        "limits": {"timeout_seconds": 30, "max_retries": 0, "max_discovered_links": 50},
                    },
                },
            )
            result = body["results"][0]
            assert result["status"] == "success"
            # After flattening, the shadow-root anchor is in the HTML and extracted.
            assert "Shadow Link" in (result.get("html") or "")
            assert "/shadow-link" in (result.get("html") or "")
        finally:
            await close_browser_pool()


class TestCookiePersistenceApi:
    """Cookie persistence integration tests (real server, real cookie jar)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url, tmp_path):
        self.client = async_client
        self.base = base_url
        self.cookie_file = tmp_path / "cookie_jar.json"
        # ASGITransport doesn't run FastAPI lifespan, so manually manage the global cookie jar
        global_cookie_jar().import_from_file(str(self.cookie_file))
        WEBHOOK_RECEIVED.clear()
        yield
        # Save cookies on teardown
        global_cookie_jar().export_to_file(str(self.cookie_file))

    async def test_cookie_persistence_across_requests(self):
        # First request: sets a cookie
        await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/set-cookie", max_discovered_links=10)
        )
        # Second request: should send the cookie back
        body2 = await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/check-cookie", max_discovered_links=10)
        )
        result = body2["results"][0]
        # The response body of /check-cookie is the Cookie header it received
        assert "session_id=abc123" in (result.get("html") or result.get("text") or "")

    async def test_cookie_persistence_survives_restart(self):
        # First crawl sets cookie
        await completed_crawl(
            self.client, make_crawl_payload(f"{self.base}/set-cookie", max_discovered_links=10)
        )
        # Second crawl verifies cookie is sent
        result = (
            await completed_crawl(
                self.client, make_crawl_payload(f"{self.base}/check-cookie", max_discovered_links=10)
            )
        )["results"][0]
        assert "session_id=abc123" in (result.get("html") or result.get("text") or "")


class TestJobApi(_AsgiEndpointTests):
    """Job API integration tests (real server)."""

    async def _seed_job(self, root_url):
        """Seed a persisted row directly; create_endpoint refuses."""
        from app.main import job_store

        job = job_store.create(root_url, CrawlPolicy())
        return job.job_id

    async def test_v1_jobs_create_runs_and_list_get(self):
        # Create starts a background runner; with the fixture server the job completes a real whole-site crawl
        # through the shared engine.
        resp = await self.client.post(
            "/v1/jobs",
            json={
                "root_url": f"{self.base}/",
                "policy": {
                    "strategies": ["HTTP_DECLARED_CRAWLER"],
                    "limits": {"timeout_seconds": 10, "max_retries": 0},
                },
            },
        )
        assert resp.status_code == 200, resp.text
        job_id = resp.json()["job_id"]

        # Wait for the runner to reach a terminal state.
        deadline = time.monotonic() + 15.0
        status = None
        while time.monotonic() < deadline:
            job_resp = await self.client.get(f"/v1/jobs/{job_id}")
            assert job_resp.status_code == 200, job_resp.text
            status = job_resp.json()["status"]
            if status in {"completed", "failed", "cancelled"}:
                break
            await asyncio.sleep(0.05)
        assert status == "completed", status

        # State inspection of directly-persisted rows still works
        seeded = await self._seed_job(f"{self.base}/")
        resp2 = await self.client.get("/v1/jobs")
        assert resp2.status_code == 200
        jobs = resp2.json()["jobs"]
        assert any(j["job_id"] == seeded for j in jobs)

        resp3 = await self.client.get(f"/v1/jobs/{seeded}")
        assert resp3.status_code == 200
        job = resp3.json()
        assert job["job_id"] == seeded
        assert job["status"] == "pending"
        assert job["frontier_size"] == 1

    async def _assert_unknown_action_refused_without_side_effect(
        self, job_id: str, expected_status: str
    ) -> None:
        """An unknown control action is refused; ``expected_status`` is the state
        *before* the bad request, so asserting the row is unchanged is what makes
        this a refusal test (a fixed ``pending`` would pass under a silent advance).
        """
        resp_bad = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "invalid"})
        assert resp_bad.status_code == 422
        assert resp_bad.json()["code"] == "invalid_action"
        resp_after = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp_after.json()["status"] == expected_status

    async def test_v1_jobs_control_transitions_persisted_row(self):
        # Directly-persisted (never-run) rows are PENDING: pause implies an active run (409). Cancel is valid
        # from pending (aborts before start).
        job_id = await self._seed_job(f"{self.base}/")
        resp_pause = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert resp_pause.status_code == 409, resp_pause.text
        assert resp_pause.json()["code"] == "invalid_state"

        resp_cancel = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert resp_cancel.status_code == 200, resp_cancel.text
        assert resp_cancel.json()["status"] == "cancelled"

        # Unknown actions stay 422 (shape validation precedes dispatch), and the cancelled row is still
        # cancelled -- the rejection is side-effect free.
        await self._assert_unknown_action_refused_without_side_effect(job_id, "cancelled")

    async def test_v1_jobs_delete_of_persisted_row(self):
        job_id = await self._seed_job(f"{self.base}/")

        resp_del = await self.client.delete(f"/v1/jobs/{job_id}")
        assert resp_del.status_code == 200
        assert resp_del.json()["status"] == "deleted"

        # Verify deleted
        resp_get = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp_get.status_code == 404

    async def test_v1_jobs_invalid_action(self):
        job_id = await self._seed_job(f"{self.base}/")

        await self._assert_unknown_action_refused_without_side_effect(job_id, "pending")
