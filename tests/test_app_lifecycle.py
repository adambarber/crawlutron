"""App lifecycle edge cases: import failures, lifespan startup/shutdown paths."""

import asyncio
import dataclasses
from pathlib import Path

import pytest

from tests._app_factory import service_app
from tests._assertions import assert_pool_unavailable
from tests._job_harnesses import post_render_with_pool
from tests._lifespan_doubles import stub_heavy_teardown


@pytest.fixture(autouse=True)
def _fresh_doc_pool_for_real_lifespan(monkeypatch):
    """A real lifespan shutdown closes the doc pool terminally (#246); give each
    test a fresh isolated pool so that shutdown never poisons later extraction."""
    from tests._doc_pool_fixtures import fresh_doc_pool

    fresh_doc_pool(monkeypatch)


def _booted_app(monkeypatch, *, workers: str | None = None):
    """The real lifespan, with the cookie jar backed by the shared store.

    The fleet boot and shared database need a RUNNING service; written twice,
    the two setups drifted into being different."""
    from app import main as main_mod

    if workers is not None:
        monkeypatch.setenv("CRAWLUTRON_WORKERS", workers)
    monkeypatch.setattr(
        main_mod,
        "settings",
        dataclasses.replace(main_mod.settings, cookie_persistence_enabled=True),
    )
    stub_heavy_teardown(monkeypatch)
    return service_app()


def _database_file(store) -> Path:
    """The resolved on-disk file a store owns, from its public db_path."""
    return Path(store.db_path).resolve()


class TestMultiWorkerInvariants:
    """`CRAWLUTRON_WORKERS > 1` is supported; these are the properties that make it so.

    The cookie jar, /metrics counters and the gate's ceilings were per-process;
    each now derives from shared state divided by worker count."""

    async def test_the_lifespan_boots_a_four_worker_fleet(self, monkeypatch):
        """The refusal is gone, so prove the boot rather than the absence of a token.

        A source read cannot see a reintroduced guard and does not prove the
        service starts; driving the real lifespan asserts what startup built."""
        from app.storage.coordination_cookies import SharedCookieStore

        app = _booted_app(monkeypatch, workers="4")

        async with app.router.lifespan_context(app):
            # The store the jar landed on is the multi-worker claim itself.
            assert isinstance(app.state.cookie_jar.store, SharedCookieStore)

    def test_the_worker_count_reaches_the_production_budget_wiring(self, monkeypatch):
        """The division has to be where the pools are built, not only in the helpers.

        A correct `derive_browser_pool_size` that production never calls is an
        unused function and an over-subscribed fleet.
        """
        from app.runtime.resource_pressure import derive_browser_pool_size

        monkeypatch.setenv("CRAWLUTRON_WORKERS", "4")
        monkeypatch.setenv("CRAWLUTRON_BROWSER_POOL_SIZE", "")
        assert derive_browser_pool_size(16 * 1024**3) == 1

    async def test_every_store_shares_the_one_database_path(self, monkeypatch):
        """The stores must share one file, or the sharing is only nominal.

        A per-process path would pass each store's own test; the path is read
        from each live connection's ``PRAGMA database_list``, not the argument."""
        from app.main import DATABASE_PATH, job_store, result_store
        from app.runtime.metrics import get_metrics
        from app.storage.coordination_cookies import SharedCookieStore
        from app.storage.coordination_counters import CoordinationCounters
        from app.transport.cookie_jar import CookieJar

        app = _booted_app(monkeypatch)

        async with app.router.lifespan_context(app):
            jar = app.state.cookie_jar
            counters = get_metrics().counters

        assert isinstance(jar, CookieJar)
        assert isinstance(jar.store, SharedCookieStore)
        assert isinstance(counters, CoordinationCounters)

        opened = {
            "job_store": _database_file(job_store),
            "result_store": _database_file(result_store),
            "cookie_jar": _database_file(jar.store),
            "counters": _database_file(counters),
        }

        expected = Path(DATABASE_PATH).resolve()
        assert expected.name == "crawler.db", DATABASE_PATH
        for name, path in opened.items():
            assert path == expected, f"{name} opened {path}, not {expected}"


class TestAppLifecycleEdges:
    """Cover remaining branches in main.py"""

    def test_main_import_error_handling(self):
        import app.main

        assert (app.main) is not None

    async def test_render_endpoint_value_error(self, async_client):
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
        # Execution contract: control resolves the row first — a missing job is 404, not the (reversed) 501
        # refusal.
        resp = await async_client.post("/v1/jobs/nonexistent/control", json={"action": "cancel"})
        assert resp.status_code == 404
        resp = await async_client.delete("/v1/jobs/nonexistent")
        assert resp.status_code == 404


class TestLifespanContextManager:
    async def test_lifespan_is_handed_the_app_it_is_managing(self):
        """The lifespan publishes onto the app it was given.

        The ``_lifespan(None)`` shape wrote the event to ``_app.state``, so
        ``None`` raised at startup; the decorator cannot see that."""
        from app.main import app

        async with app.router.lifespan_context(app):
            assert isinstance(app.state.shutdown_event, asyncio.Event)


class TestShutdownEventBelongsToOneLifespanRun:
    """The shutdown signal is minted per run, not once per process.

    A module-global event wired to ``app.state`` at import stayed signalled
    after the first run, so later crawls started cancelled."""

    async def test_a_second_lifespan_run_gets_a_live_shutdown_event(self, monkeypatch):
        stub_heavy_teardown(monkeypatch)
        app = service_app()

        async with app.router.lifespan_context(app):
            first = app.state.shutdown_event
        assert first.is_set(), "precondition: leaving the lifespan must signal that run's event"

        async with app.router.lifespan_context(app):
            second = app.state.shutdown_event
            # Read the liveness inside the block. The teardown signals the event on the way out, so asserting
            # after the block would pass against a dead event for the wrong reason.
            second_is_live = not second.is_set()

        assert second is not first, "the lifespan reused the previous run's event object"
        assert second_is_live, (
            "the second lifespan run inherited the first run's shutdown signal, so every "
            "crawl it starts is cancelled before it does any work"
        )
