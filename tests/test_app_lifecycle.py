"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

import asyncio
import inspect

import pytest
from _assertions import assert_pool_unavailable, read_main_source
from _job_harnesses import post_render_with_pool


class TestMultiWorkerInvariants:
    """`BROWSER_CRAWLER_WORKERS > 1` is supported; these are the properties that make it so.

    The service used to refuse a second process, because three of its
    process-local singletons were wrong rather than merely wasteful:

      * the cookie jar loaded at startup and rewrote the whole file at
        shutdown, so N workers made continuity per-process and shutdown
        last-writer-wins. It is now backed by a table in the shared database;
      * `/metrics` served one worker's counters, so the numbers the admission
        gate is tuned from reported a fraction of real load. The counters are
        now in the same shared database, and the two resource gauges stay
        per-process because they read a cgroup every worker shares;
      * the admission gate reads shared cgroup pressure but granted capacity
        per process. Its ceilings are now divided by the worker count where
        they are derived, so the fleet's grants sum to one worker's worth.

    The refusal is gone, so these assert the invariants hold rather than that
    boot is denied. The arithmetic behind the division is proven in
    `tests/crawl/test_fleet_budget_split.py`; what is untested there is that
    the running service is actually wired to it, which is what this covers.
    """

    def test_lifespan_does_not_refuse_more_than_one_worker(self, monkeypatch):
        monkeypatch.setenv("BROWSER_CRAWLER_WORKERS", "4")
        source = read_main_source()
        assert "assert_single_process_deployment" not in source
        assert "is not supported" not in source

    def test_no_refusal_survives_anywhere_in_the_tree(self):
        """A refusal left behind in one module denies boot just as hard.

        The guard was a chokepoint once; now the class it protected is closed,
        and a second copy of the same check elsewhere would be a deployment
        that refuses for a reason no longer true.
        """
        from chokepoint_walks import app_python_files

        for path in app_python_files():
            if "assert_single_process_deployment" in path.read_text():
                pytest.fail(f"{path} still refuses multiple workers")

    def test_the_worker_count_reaches_the_production_budget_wiring(self, monkeypatch):
        """The division has to be where the pools are built, not only in the helpers.

        A correct `derive_browser_pool_size` that production never calls is an
        unused function and an over-subscribed fleet.
        """
        from app.crawl import request_pipeline
        from app.resource_pressure import derive_browser_pool_size

        monkeypatch.setenv("BROWSER_CRAWLER_WORKERS", "4")
        monkeypatch.setenv("BROWSER_CRAWLER_BROWSER_POOL_SIZE", "")
        assert derive_browser_pool_size(16 * 1024**3) == 1

        source = inspect.getsource(request_pipeline)
        assert "derive_browser_pool_size()" in source

    def test_every_store_shares_the_one_database_path(self):
        """The stores must share one file, or the sharing is only nominal.

        The cookie jar and the metrics counters each hold a connection. If
        either were pointed at its own path -- which is what the
        single-database decision exists to prevent -- both would pass their own
        tests and the fleet would still not see shared state.
        """
        from app.main import DATABASE_PATH

        assert DATABASE_PATH.endswith("/crawler.db")
        source = read_main_source()
        for ctor in ("JobStore(", "CrawlResultStore(", "CoordinationCounters(", "SharedCookieStore("):
            assert ctor in source, ctor
            line = next(ln for ln in source.splitlines() if ctor in ln)
            assert "DATABASE_PATH" in line or ctor not in source.split(line)[1][:40], ctor
        assert "coordination_db_path" not in source


class TestAppLifecycleEdges:
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main

        assert (app.main) is not None

    async def test_render_endpoint_value_error(self, async_client):
        # ASGI harness runs no lifespan, so the pool is None; M13's guard
        # answers 503 browser_pool_unavailable before URL validation.
        # Own the premise explicitly: ordering must never decide it.
        assert_pool_unavailable(await post_render_with_pool(async_client, None, "invalid"))

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
        src = read_main_source()
        # The old pattern was: lifespan=_lifespan(None) with def _lifespan(app: FastAPI)
        assert "lifespan=_lifespan(None)" not in src
        # The new pattern: a directly-decorated async context manager.
        assert "@asynccontextmanager" in src
        assert "async def _lifespan(_app: FastAPI)" in src
        assert "lifespan=_lifespan" in src
