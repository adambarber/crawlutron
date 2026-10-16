"""Discovery/health endpoints and the legacy /v1/render path."""

import pytest
from fastapi import Request
from fastapi.exceptions import RequestValidationError

from tests._assertions import assert_pool_unavailable
from tests._browser_doubles import RaisingRendererPool
from tests._job_harnesses import post_render_with_pool


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
        # Saturation must come from the live resource gate. No crawl has run here, so the metrics gauges are
        # stale zeros -- a sampler reporting saturation must still yield 503.
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
        # With a typed request body, a malformed body shape is rejected by FastAPI with 422 before reaching
        # the handler.
        resp = await self.client.post("/v1/render", json={"options": "not-a-dict"})
        assert resp.status_code == 422

    async def test_v1_render_exhausted_pool_returns_503(self, monkeypatch):
        # When every renderer is wedged, the request surfaces as 503, not a hang or a 500.
        import app.api.render_endpoint as render_endpoint
        from app.orchestration.render_result import crawl_result_from_exception
        from app.transport.browser_pool import BrowserPoolExhaustedError
        from app.vocabulary.strategies import BROWSER_HEADLESS

        class _ExhaustedWorker:
            def __init__(self, pool):
                pass

            async def __call__(self, job):
                try:
                    raise BrowserPoolExhaustedError("pool exhausted in test")
                except Exception as error:
                    return crawl_result_from_exception(
                        error=error,
                        url=job.url,
                        domain=job.domain,
                        strategy=BROWSER_HEADLESS,
                        elapsed_ms=0,
                    )

        monkeypatch.setattr(render_endpoint, "PooledBrowserWorker", _ExhaustedWorker)
        assert_pool_unavailable(await post_render_with_pool(self.client, object(), "http://127.0.0.1:1/x"))

    async def test_v1_render_worker_exception_returns_shaped_failure_not_500(self, monkeypatch):
        # The render pipeline normalizes the shape at its single receive point and routes the endpoint through
        # the same per-item shaping guard /v1/crawl has, so the failure degrades to a correctly-shaped
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
        # RuntimeError classifies to the stable network_error code, and the raw text never reaches the wire.
        assert item["classification"] == "network_error", item
        assert secret not in resp.text, f"raw exception text leaked into the response: {resp.text!r}"

    async def test_v1_render_reports_the_real_failure_for_a_worker_exception(self, monkeypatch):
        import app.api.render_endpoint as render_endpoint
        from app.transport.browser_pool import PooledBrowserWorker

        monkeypatch.setattr(render_endpoint, "PooledBrowserWorker", lambda pool: PooledBrowserWorker(pool))
        resp = await post_render_with_pool(self.client, RaisingRendererPool(), "http://127.0.0.1:1/x")
        assert resp.status_code == 200, resp.text
        item = resp.json()["results"][0]
        # The wire says why the render failed: the classification the shared chokepoint gave the exception,
        # not the shaping guard's fallback.
        assert item["classification"] == "network_error", item
        assert item["error"] != "result_shaping_failed", item

    async def test_v1_render_without_pool_returns_503(self):
        # A None browser pool (startup/shutdown window) must surface as a clean 503, not an AttributeError 500
        # from inside the worker.
        resp = await post_render_with_pool(self.client, None, "http://127.0.0.1:1/x")
        assert resp.status_code == 503
        assert resp.json()["code"] == "browser_pool_unavailable"

    def _warmed_pool(self, monkeypatch, warmup_urls):
        """A real ``/v1/render`` whose pool serves renders in a chosen mode; only
        the browser is faked (no binary in CI), so a key the adapter emits but
        the wire model drops never reaches the assertion.
        """
        from app.models.crawl_result import CrawlResult
        from app.orchestration.render_result import terminal_attempt
        from app.transport.browser_pool import BrowserPool

        class _FakeRenderer:
            async def render(self, url, options, *, policy=None):
                # The real renderer does not know the pool's configured mode, so it returns a result without
                # one -- the worker fills the slot.
                return CrawlResult(
                    url=url,
                    attempt=terminal_attempt(
                        strategy=None,
                        status="success",
                        classification="rendered",
                        elapsed_ms=1,
                        error=None,
                    ),
                    final_url=url,
                    html="<html></html>",
                    text="t",
                    attempts=1,
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
        """The wire must carry the mode that actually served the render: the adapter
        once hard-coded ``BROWSER_HEADLESS`` while a warmed pool served
        ``BROWSER_HEADED_WARMED``, so two endpoints disagreed on the provenance.
        """
        from app.vocabulary.strategies import BROWSER_HEADED_WARMED

        item = await self._render_result(monkeypatch, ["https://warmup.example/"])
        assert item["selected_strategy"] == BROWSER_HEADED_WARMED, item
        assert item["attempted_strategies"][0]["strategy"] == BROWSER_HEADED_WARMED, item

    async def test_v1_render_reports_headless_without_warmup(self, monkeypatch):
        """Inverse case: a plain headless pool still labels its own mode, not the
        warmed member."""
        from app.vocabulary.strategies import BROWSER_HEADLESS

        item = await self._render_result(monkeypatch, [])
        assert item["selected_strategy"] == BROWSER_HEADLESS, item
        assert item["attempted_strategies"][0]["strategy"] == BROWSER_HEADLESS, item

    async def test_v1_render_rejects_invalid_url_before_browser(self, monkeypatch):
        pool = self._warmed_pool(monkeypatch, ["https://warmup.example/"])
        resp = await post_render_with_pool(self.client, pool, "not a url")
        assert resp.status_code == 400, resp.text
        assert resp.json()["code"] == "validation_error"

    async def test_validation_exception_handler_returns_422(self):
        # The handler itself, not the one in the app's registry.
        from app.api.exceptions import validation_exception_handler

        scope = {"type": "http", "method": "POST", "path": "/v1/crawl", "headers": [], "query_string": b""}
        request = Request(scope)
        exc = RequestValidationError(
            errors=[{"loc": ("body", "url"), "msg": "field required", "type": "value_error.missing"}]
        )
        response = await validation_exception_handler(request, exc)
        assert response.status_code == 422
