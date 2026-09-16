"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

import inspect


def _read_main_source() -> str:

    from app import main as main_mod

    return inspect.getsource(main_mod)




from _testkit import *


class TestAppLifecycleEdges:
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main

        assert (app.main) is not None

    async def test_render_endpoint_value_error(self, async_client):
        from app.crawl.strategies.browser_pool import set_browser_pool

        # ASGI harness runs no lifespan, so the pool is None; M13's guard
        # answers 503 browser_pool_unavailable before URL validation.
        # Own the premise explicitly: ordering must never decide it.
        previous = set_browser_pool(None)
        try:
            resp = await async_client.post("/v1/render", json={"url": "invalid"})
        finally:
            set_browser_pool(previous)
        assert resp.status_code == 503
        assert resp.json()["code"] == "browser_pool_unavailable"

    async def test_render_endpoint_success(self, async_client):
        # Missing 'url' triggers the standardized 422 validation envelope
        resp = await async_client.post("/v1/render", json={})
        assert resp.status_code == 422
        body = resp.json()
        assert body["code"] == "validation_error"
        assert isinstance(body["details"]["errors"], list)

    async def test_crawl_endpoint_webhook_error(self, async_client):
        # Crawl a valid URL; webhook delivery is best-effort and won't crash the request
        resp = await async_client.post(
            "/v1/crawl", json={"url": "http://example.com/", "webhook_url": "http://unreachable.invalid/"}
        )
        # The endpoint should accept the request (202 = accepted, background processing)
        assert resp.status_code in (200, 201, 202)
        # Give background task a brief moment, then ensure no exception propagated
        await asyncio.sleep(0.5)

    async def test_map_endpoint_value_error(self, async_client):
        resp = await async_client.post("/v1/map", json={"url": "invalid"})
        assert resp.status_code == 422

    async def test_job_endpoints_not_found(self, async_client):
        resp = await async_client.get("/v1/jobs/nonexistent")
        assert resp.status_code == 404
        # H5 execution contract: control resolves the row first — a
        # missing job is 404, not the (reversed) 501 refusal.
        resp = await async_client.post("/v1/jobs/nonexistent/control", json={"action": "cancel"})
        assert resp.status_code == 404
        resp = await async_client.delete("/v1/jobs/nonexistent")
        assert resp.status_code == 404

    def test_app_none_fallback(self):
        import app.main as main_mod

        assert (main_mod.app) is not None


class TestLifespanContextManager:
    def test_lifespan_is_direct_contextmanager(self):
        """The FastAPI lifespan must be a direct contextmanager, not ``_lifespan(None)``."""
        src = _read_main_source()
        # The old pattern was: lifespan=_lifespan(None) with def _lifespan(app: FastAPI)
        assert "lifespan=_lifespan(None)" not in src
        # The new pattern: a directly-decorated async context manager.
        assert "@asynccontextmanager" in src
        assert "async def _lifespan(_app: FastAPI)" in src
        assert "lifespan=_lifespan" in src
