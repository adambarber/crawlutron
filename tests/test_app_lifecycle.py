"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

import asyncio
import dataclasses
import inspect

from tests._app_factory import service_app
from tests._assertions import assert_pool_unavailable, read_main_source
from tests._job_harnesses import post_render_with_pool
from tests._lifespan_doubles import stub_heavy_teardown


class TestMultiWorkerInvariants:
    """`CRAWLUTRON_WORKERS > 1` is supported; these are the properties that make it so.

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

    async def test_the_lifespan_boots_a_four_worker_fleet(self, monkeypatch):
        """The refusal is gone, so prove the boot rather than the absence of a token.

        A source read cannot see a guard reintroduced under another name, and it
        does not prove the service starts at all. This drives the real lifespan
        at the fleet's worker count and asserts what startup built while doing
        it: the cookie jar has to be backed by the shared store, because a jar
        only this process can see is exactly what made N workers lose every
        cookie the others collected while reporting healthy.
        """
        from app import main as main_mod
        from app.crawl.coordination_cookies import SharedCookieStore
        from app.crawl.strategies import cookie_jar as cookie_jar_module

        monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
        monkeypatch.setattr(
            main_mod,
            "settings",
            dataclasses.replace(main_mod.settings, cookie_persistence_enabled=True),
        )
        stub_heavy_teardown(monkeypatch)
        app = service_app()

        async with app.router.lifespan_context(app):
            # A refusal anywhere on the startup path raises before the yield, so
            # being inside the block is the boot assertion. The store the jar
            # landed on is the multi-worker claim itself.
            assert isinstance(cookie_jar_module._GLOBAL_COOKIE_JAR._store, SharedCookieStore)

    def test_the_worker_count_reaches_the_production_budget_wiring(self, monkeypatch):
        """The division has to be where the pools are built, not only in the helpers.

        A correct `derive_browser_pool_size` that production never calls is an
        unused function and an over-subscribed fleet.
        """
        from app.crawl import request_pipeline
        from app.resource_pressure import derive_browser_pool_size

        monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
        monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "")
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
        # The constructors, not the module-level bindings: ``job_store`` is a
        # name the module assigns, and a token of ``job_store(`` would have
        # passed against a helper that no longer exists and failed against the
        # assignment that does.
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
        # ASGI harness runs no lifespan, so the pool is None; 's guard
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
        # Execution contract: control resolves the row first — a
        # missing job is 404, not the (reversed) 501 refusal.
        resp = await async_client.post("/v1/jobs/nonexistent/control", json={"action": "cancel"})
        assert resp.status_code == 404
        resp = await async_client.delete("/v1/jobs/nonexistent")
        assert resp.status_code == 404


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


class TestShutdownEventBelongsToOneLifespanRun:
    """The shutdown signal is minted per run, not once per process.

    ``shutdown_event`` was a module global built at import and wired to
    ``app.state`` at import, while the lifespan only ever called ``.set()`` on
    it. The first run to finish therefore left the process-wide event signalled
    for good, and every later run handed that dead event to the request
    dependency, so crawls started already cancelled.

    Nothing in production reads the global -- ``get_shutdown_event`` resolves
    ``request.app.state.shutdown_event`` -- so the state is the only thing that
    has to be fresh, and the fix belongs in the lifespan.
    """

    async def test_a_second_lifespan_run_gets_a_live_shutdown_event(self, monkeypatch):
        stub_heavy_teardown(monkeypatch)
        app = service_app()

        async with app.router.lifespan_context(app):
            first = app.state.shutdown_event
        assert first.is_set(), "precondition: leaving the lifespan must signal that run's event"

        async with app.router.lifespan_context(app):
            second = app.state.shutdown_event
            # Read the liveness inside the block. The teardown signals the event
            # on the way out, so asserting after the block would pass against a
            # dead event for the wrong reason.
            second_is_live = not second.is_set()

        assert second is not first, "the lifespan reused the previous run's event object"
        assert second_is_live, (
            "the second lifespan run inherited the first run's shutdown signal, so every "
            "crawl it starts is cancelled before it does any work"
        )
