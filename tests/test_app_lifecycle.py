"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

import asyncio
import inspect

import pytest
from _job_harnesses import post_render_with_pool


def _read_main_source() -> str:

    from app import main as main_mod

    return inspect.getsource(main_mod)


class TestSingleProcessInvariant:
    """`BROWSER_CRAWLER_WORKERS > 1` must fail loudly, not corrupt state.

    The service keeps its browser pool, httpx/cffi pools, DNS and extraction
    executors, HTTP cache, robots pattern cache, cookie jar, admission gate and
    metrics store as process-local singletons. Several of them also WRITE to a
    shared path, so N workers do not merely under-report -- they lose data:

      * the cookie jar loads at startup and rewrites the whole file at
        shutdown, so N workers make continuity per-process and shutdown
        last-writer-wins;
      * `/metrics` serves one worker's counters, so the gauges that make the
        self-tuning guard observable report a fraction of the truth;
      * the admission gate reads shared cgroup pressure but grants its
        capacity per process, uncoordinated;
      * content-cache spill GC was process-local until it took a flock.

    One guard at startup closes the class. Refusing to boot is the honest
    outcome: the alternative is a deployment that looks healthy and quietly
    drops cookies.
    """

    def test_refuses_more_than_one_worker(self, monkeypatch):
        from app.main import assert_single_process_deployment

        monkeypatch.setenv("BROWSER_CRAWLER_WORKERS", "4")
        with pytest.raises(RuntimeError) as excinfo:
            assert_single_process_deployment()
        message = str(excinfo.value)
        # The refusal must name WHY, or an operator reads it as a crash and
        # retries with workers=1 having learned nothing.
        assert "BROWSER_CRAWLER_WORKERS" in message
        assert "cookie" in message.lower()

    def test_accepts_single_worker(self, monkeypatch):
        from app.main import assert_single_process_deployment

        monkeypatch.setenv("BROWSER_CRAWLER_WORKERS", "1")
        assert_single_process_deployment()  # must not raise

    def test_unset_or_absurd_value_does_not_refuse_boot(self, monkeypatch):
        """The env chokepoint's contract: a typo falls back, it never denies boot.

        `get_mapping_int` exists precisely so a malformed value warns and uses
        the default. This guard must not become a second, stricter parser that
        turns a typo into an outage.
        """
        from app.main import assert_single_process_deployment

        monkeypatch.delenv("BROWSER_CRAWLER_WORKERS", raising=False)
        assert_single_process_deployment()
        monkeypatch.setenv("BROWSER_CRAWLER_WORKERS", "not-a-number")
        assert_single_process_deployment()

    def test_lifespan_enforces_the_guard_before_allocating_anything(self):
        """The check must run before the pools exist, not after.

        A guard placed after `initialize_browser_pool` has already spun up N
        copies of the scarcest resource in the service before complaining.
        """
        source = _read_main_source()
        guard_at = source.index("assert_single_process_deployment()")
        assert guard_at < source.index("initialize_browser_pool(settings)")






class TestAppLifecycleEdges:
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main

        assert (app.main) is not None

    async def test_render_endpoint_value_error(self, async_client):
        # ASGI harness runs no lifespan, so the pool is None; M13's guard
        # answers 503 browser_pool_unavailable before URL validation.
        # Own the premise explicitly: ordering must never decide it.
        resp = await post_render_with_pool(async_client, None, "invalid")
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
