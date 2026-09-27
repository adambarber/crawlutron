"""End-to-end /v1/crawl behavior against a real local HTTP server (no mocks)."""

import asyncio
import json
import time

import pytest
from _assertions import (
    accepted_crawl,
    assert_pool_unavailable,
    assert_result_failed,
    browser_available,
    completed_crawl,
)
from _fetcher_factories import make_crawl_payload
from _job_harnesses import post_render_with_pool
from _policy_factories import crawl_policy
from helpers.server import WEBHOOK_RECEIVED

from app.constants import HTTP_DECLARED_CRAWLER
from app.main import app

#: Discovery turned fully on. Three tests assert on what robots, sitemaps and
#: feeds contributed, and spelling the triple at each is how one of them kept
#: asking for feeds the fixture never served.
DISCOVERY_ALL = {"robots": True, "sitemaps": True, "feeds": True}

#: A scope the loopback fixture cannot match -- the point of the scope tests is
#: that the discovered links are *outside* it.
EXAMPLE_SCOPE = r"^https://example\.com"

#: Room for 100 links and one level of depth, so the scope is the only thing
#: deciding what comes back.
WIDE_LIMITS = {"max_discovered_links": 100, "max_depth": 1}


class _AsgiEndpointTests:
    """The ASGI client plus the fixture-server base URL, for endpoint classes.

    Four of the endpoint classes below opened with this identical autouse
    fixture, and a copy of it is a rule with two owners: a class that forgot the
    ``WEBHOOK_RECEIVED.clear()`` inherited a webhook assertion reading another
    test's delivery. It lives here once, and a class that needs a different
    fixture (no base URL, or one that also persists a cookie jar) keeps its own
    rather than inheriting a superset it does not want.
    """

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url):
        self.client = async_client
        self.base = base_url
        WEBHOOK_RECEIVED.clear()


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
        # #2-fix: plain single-page crawl skips link extraction entirely
        assert result["discovered_link_count"] == 0
        assert result["discovered_links"] == []
        # next_page_url stays a standard field regardless
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
        result = (await completed_crawl(self.client, make_crawl_payload(f"{self.base}/missing")))["results"][0]
        assert_result_failed(result, "not_found")

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
        # The background task crawls + POSTs the result to the webhook sink.
        assert len(WEBHOOK_RECEIVED) == 1
        delivered = json.loads(WEBHOOK_RECEIVED[0].decode("utf-8"))
        assert delivered["submitted_count"] == 1
        assert "results" in delivered

    async def test_metrics_endpoint_exposes_prometheus_text(self):
        await self.client.post("/v1/crawl", json=make_crawl_payload(f"{self.base}/"))
        resp = await self.client.get("/metrics")
        assert resp.status_code == 200, resp.text
        assert "crawler_crawl_requests_total" in resp.text
        assert "crawler_crawl_results_total" in resp.text

    async def test_v1_crawl_jsonl_output_format(self):
        # #17: output_format=jsonl returns newline-delimited JSON (one object per URL).
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

        resp = build_unified_crawl_response(
            [
                {
                    "original_url": "http://x/",
                    "final_url": "http://x/",
                    "status": "success",
                    "classification": "rendered",
                }
            ]
        )
        assert resp.success is True
        assert resp.submitted_count == 1

    async def test_v1_render_invalid_payload_rejected(self):
        # With a typed request body, a malformed body shape is rejected by
        # FastAPI with 422 before reaching the handler.
        resp = await self.client.post("/v1/render", json={"options": "not-a-dict"})
        assert resp.status_code == 422

    async def test_v1_render_exhausted_pool_returns_503(self, monkeypatch):
        # M12: when every renderer is wedged, the request surfaces as 503, not
        # a hang or a 500.
        #
        # The fake mirrors the production worker's contract: pool exhaustion is
        # caught and returned as a classified terminal CrawlResult, never
        # raised (a raised exhaustion used to escape to the scheduler, whose
        # envelope is the wrong shape for the render pipeline). So the 503 is
        # reached through the real failure path -- render_request reading the
        # browser_pool_exhausted classification off the result -- and not
        # through the incidental route the old raising fake exploited.
        import app.api.render_endpoint as render_endpoint
        from app.constants import BROWSER_HEADLESS
        from app.crawl.result_helpers import _crawl_result_from_exception
        from app.crawl.strategies.browser_pool import BrowserPoolExhaustedError

        class _ExhaustedWorker:
            def __init__(self, pool):
                pass

            async def __call__(self, job):
                try:
                    raise BrowserPoolExhaustedError("pool exhausted in test")
                except Exception as error:
                    return _crawl_result_from_exception(
                        error=error,
                        url=job.url,
                        domain=job.domain,
                        strategy=BROWSER_HEADLESS,
                        elapsed_ms=0,
                    )

        monkeypatch.setattr(render_endpoint, "PooledBrowserWorker", _ExhaustedWorker)
        assert_pool_unavailable(
            await post_render_with_pool(self.client, object(), "http://127.0.0.1:1/x")
        )

    async def test_v1_render_worker_exception_returns_shaped_failure_not_500(self, monkeypatch):
        # A render worker that raises a plain Exception (not pool exhaustion)
        # lands in the scheduler's blanket envelope, which is an
        # EngineCrawlResult -- the right shape for /v1/crawl, wrong for this
        # pipeline: render_result_to_unified reads item["url"], which that
        # shape does not carry, so the response used to die with a KeyError
        # and surface as a 500. The render pipeline normalizes the shape at its
        # single receive point and routes the endpoint through the same
        # per-item shaping guard /v1/crawl has, so the failure degrades to a
        # correctly-shaped terminal result instead.
        #
        # PooledBrowserWorker catches every Exception itself, so this is latent
        # for the production worker -- but the P0 getattr fix silenced mypy's
        # only static signal of the polymorphism, so nothing else catches a
        # future caller that lets one escape.
        import app.api.render_endpoint as render_endpoint

        secret = "SECRET_TOKEN_ABC123_INTERNAL_PATH=/etc"

        class _RaisingWorker:
            def __init__(self, pool):
                pass

            async def __call__(self, job):
                raise RuntimeError(f"boom: {secret}")

        monkeypatch.setattr(render_endpoint, "PooledBrowserWorker", _RaisingWorker)
        resp = await post_render_with_pool(self.client, object(), "http://127.0.0.1:1/x")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["submitted_count"] == 1
        assert len(body["results"]) == 1, "the failing URL must not drop out of the batch"
        item = body["results"][0]
        assert item["original_url"] == "http://127.0.0.1:1/x", item
        assert item["status"] == "failed", item
        # RuntimeError classifies to the stable network_error code, and the
        # raw text never reaches the wire.
        assert item["classification"] == "network_error", item
        assert secret not in resp.text, f"raw exception text leaked into the response: {resp.text!r}"

    async def test_v1_render_without_pool_returns_503(self):
        # M13: a None browser pool (startup/shutdown window) must surface as
        # a clean 503, not an AttributeError 500 from inside the worker.
        resp = await post_render_with_pool(self.client, None, "http://127.0.0.1:1/x")
        assert resp.status_code == 503
        assert resp.json()["code"] == "browser_pool_unavailable"

    def _warmed_pool(self, monkeypatch, warmup_urls):
        """A real ``/v1/render`` whose pool serves renders in a chosen mode.

        Only the browser is faked (no binary in CI): the pool, its
        ``browser_strategy`` property, the pooled worker, the scheduler, the
        ``to_dict`` serialization, the ``render_result_to_unified`` adapter and
        the strict ``UnifiedCrawlResponse`` model all run as in production, so a
        key the adapter emits but the wire model does not declare is dropped
        before it reaches the assertion.
        """
        from app.crawl.strategies.browser_pool import BrowserPool
        from app.models import CrawlResult

        class _FakeRenderer:
            async def render(self, url, options):
                # The real renderer does not know the pool's configured mode, so
                # it returns a result without one -- the worker fills the slot.
                return CrawlResult(
                    url=url,
                    status="success",
                    classification="rendered",
                    final_url=url,
                    html="<html></html>",
                    text="t",
                    attempts=1,
                    elapsed_ms=1,
                    domain="127.0.0.1",
                    metadata={"extraction_method": "browser"},
                )

        pool = BrowserPool(pool_size=1, warmup_urls=warmup_urls)
        renderer = _FakeRenderer()

        async def _acquire(*args, **kwargs):
            return renderer

        monkeypatch.setattr(pool, "acquire", _acquire)
        monkeypatch.setattr(pool, "release", lambda _renderer: None)
        return pool

    async def _render_result(self, monkeypatch, warmup_urls):
        """Post through a selected browser-pool mode and return the wire result."""
        pool = self._warmed_pool(monkeypatch, warmup_urls)
        response = await post_render_with_pool(self.client, pool, "http://127.0.0.1:1/x")
        assert response.status_code == 200, response.text
        return response.json()["results"][0]

    async def test_v1_render_reports_the_pools_real_strategy_when_warmed(self, monkeypatch):
        """The render wire must carry the browser mode that actually served the
        render. The adapter used to hard-code ``BROWSER_HEADLESS`` for every
        result while a warmed pool serves ``BROWSER_HEADED_WARMED`` -- the two
        endpoints then disagreed about the provenance of the same render."""
        from app.constants import BROWSER_HEADED_WARMED

        item = await self._render_result(monkeypatch, ["https://warmup.example/"])
        assert item["selected_strategy"] == BROWSER_HEADED_WARMED, item
        assert item["attempted_strategies"][0]["strategy"] == BROWSER_HEADED_WARMED, item

    async def test_v1_render_reports_headless_without_warmup(self, monkeypatch):
        """Inverse case: a plain headless pool still labels its own mode, not the
        warmed member."""
        from app.constants import BROWSER_HEADLESS

        item = await self._render_result(monkeypatch, [])
        assert item["selected_strategy"] == BROWSER_HEADLESS, item
        assert item["attempted_strategies"][0]["strategy"] == BROWSER_HEADLESS, item

    async def test_v1_render_rejects_invalid_url_before_browser(self, monkeypatch):
        pool = self._warmed_pool(monkeypatch, ["https://warmup.example/"])
        resp = await post_render_with_pool(self.client, pool, "not a url")
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "validation_error"

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


class TestMapApi(_AsgiEndpointTests):
    """#2 /v1/map against a real local server (no mocks)."""

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


class TestShadowDomApi(_AsgiEndpointTests):
    """#25 Shadow-DOM flattening via browser renderer (real camoufox)."""

    @pytest.mark.browser
    @pytest.mark.skipif(not browser_available(), reason="no usable browser binary (camoufox/playwright)")
    async def test_v1_crawl_shadow_dom_flattened(self):
        # The ASGI harness runs no lifespan, so the pool is None; a real
        # browser-strategy crawl needs a live pool. Initialize (real
        # camoufox) and close it in a finally so no browsers leak into
        # later tests.
        from app.crawl.strategies.browser_pool import close_browser_pool, initialize_browser_pool

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
    """#9 Cookie persistence integration tests (real server, real cookie jar)."""

    @pytest.fixture(autouse=True)
    def setup(self, async_client, base_url, tmp_path):
        self.client = async_client
        self.base = base_url
        self.cookie_file = tmp_path / "cookie_jar.json"
        # ASGITransport doesn't run FastAPI lifespan, so manually manage the global cookie jar
        from app.crawl.strategies.cookie_jar import _GLOBAL_COOKIE_JAR

        _GLOBAL_COOKIE_JAR.import_from_file(str(self.cookie_file))
        WEBHOOK_RECEIVED.clear()
        yield
        # Save cookies on teardown
        _GLOBAL_COOKIE_JAR.export_to_file(str(self.cookie_file))

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
    """#3+#14 Job API integration tests (real server)."""

    async def _seed_job(self, root_url):
        """Seed a persisted row directly; create_endpoint refuses (H5)."""
        from app.main import job_store

        job = job_store.create(root_url, {})
        return job.job_id

    async def test_v1_jobs_create_runs_and_list_get(self):
        # Create starts a background runner; with the fixture server the job
        # completes a real whole-site crawl through the shared engine.
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

    async def test_v1_jobs_control_transitions_persisted_row(self):
        # Directly-persisted (never-run) rows are PENDING: pause implies an
        # active run (409). Cancel is valid from pending (aborts before start).
        job_id = await self._seed_job(f"{self.base}/")
        resp_pause = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "pause"})
        assert resp_pause.status_code == 409, resp_pause.text
        assert resp_pause.json()["code"] == "invalid_state"

        resp_cancel = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "cancel"})
        assert resp_cancel.status_code == 200, resp_cancel.text
        assert resp_cancel.json()["status"] == "cancelled"

        # Unknown actions stay 422 (shape validation precedes dispatch).
        # The contract is the EXACT envelope code (a substring check would
        # keep passing through any code drift that keeps the prefix) and the
        # persisted row being untouched by the rejected request.
        resp_bad = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "invalid"})
        assert resp_bad.status_code == 422
        assert resp_bad.json()["code"] == "invalid_action"
        resp_after = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp_after.json()["status"] == "cancelled"

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

        resp_bad = await self.client.post(f"/v1/jobs/{job_id}/control", json={"action": "invalid"})
        assert resp_bad.status_code == 422
        # Exact code is the wire contract (jobs_router declares
        # "invalid_action"); the rejection must also leave the persisted
        # row exactly as it was -- a side-effect-free refusal.
        assert resp_bad.json()["code"] == "invalid_action"
        resp_after = await self.client.get(f"/v1/jobs/{job_id}")
        assert resp_after.json()["status"] == "pending"
