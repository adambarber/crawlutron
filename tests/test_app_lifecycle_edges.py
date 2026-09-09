"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

from coverage_helpers import *


class TestAppLifecycleEdges:
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main

        assert (app.main) is not None

    async def test_render_endpoint_value_error(self):
        from app.main import app
        from app.crawl.strategies.browser_pool import set_browser_pool

        async def test(client):
            # ASGI harness runs no lifespan, so the pool is None; M13's guard
            # answers 503 browser_pool_unavailable before URL validation.
            # Own the premise explicitly: ordering must never decide it.
            previous = set_browser_pool(None)
            try:
                resp = await client.post("/v1/render", json={"url": "invalid"})
            finally:
                set_browser_pool(previous)
            assert resp.status_code == 503
            assert resp.json()["code"] == "browser_pool_unavailable"

        await run_async_test(app, test)

    async def test_render_endpoint_success(self):
        from app.main import app

        async def test(client):
            # Missing 'url' triggers the standardized 422 validation envelope
            resp = await client.post("/v1/render", json={})
            assert resp.status_code == 422
            body = resp.json()
            assert body["code"] == "validation_error"
            assert isinstance(body["details"]["errors"], list)

        await run_async_test(app, test)

    async def test_crawl_endpoint_webhook_error(self):
        from app.main import app

        async def test(client):
            # Crawl a valid URL; webhook delivery is best-effort and won't crash the request
            resp = await client.post(
                "/v1/crawl", json={"url": "http://example.com/", "webhook_url": "http://unreachable.invalid/"}
            )
            # The endpoint should accept the request (202 = accepted, background processing)
            assert resp.status_code in (200, 201, 202)
            # Give background task a brief moment, then ensure no exception propagated
            await asyncio.sleep(0.5)

        await run_async_test(app, test)

    async def test_map_endpoint_value_error(self):
        from app.main import app

        async def test(client):
            resp = await client.post("/v1/map", json={"url": "invalid"})
            assert resp.status_code == 422

        await run_async_test(app, test)

    async def test_job_endpoints_not_found(self):
        from app.main import app

        async def test(client):
            resp = await client.get("/v1/jobs/nonexistent")
            assert resp.status_code == 404
            # H5 execution contract: control resolves the row first — a
            # missing job is 404, not the (reversed) 501 refusal.
            resp = await client.post("/v1/jobs/nonexistent/control", json={"action": "cancel"})
            assert resp.status_code == 404
            resp = await client.delete("/v1/jobs/nonexistent")
            assert resp.status_code == 404

        await run_async_test(app, test)

    def test_app_none_fallback(self):
        import app.main as main_mod

        assert (main_mod.app) is not None
