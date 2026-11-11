"""The app-lifespan spill sweep: stale released crawl_spills rows leave the
shared store on the tick, a live writer's rows survive it, and the lifespan
starts and drains the sweep task.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing

from tests._lifespan_doubles import stub_heavy_teardown


class TestSpillSweepTick:
    async def test_the_tick_reclaims_released_rows_past_the_age_and_spares_fenced_ones(self, tmp_path):
        from crawlutron.api.spill_sweep import spill_maintenance_once
        from crawlutron.storage.result_store import CrawlSpillStore
        from tests._result_builders import engine_result

        now = 1000.0
        times = {"t": now}
        store = CrawlSpillStore(tmp_path / "spills.db", clock=lambda: times["t"])
        try:
            store.put("r-old", 0, engine_result("https://spill.test/old", "rendered"))
            store.put("r-live", 0, engine_result("https://spill.test/live", "rendered"))
            with closing(sqlite3.connect(tmp_path / "spills.db")) as conn:
                conn.execute("UPDATE crawl_spills SET created_at = ?", (now - 7200,))
                conn.commit()
            store.release_spill("r-old")  # served or dead: its rows are age-reclaimable

            reclaimed = await spill_maintenance_once(store, max_age_seconds=3600.0)

            assert reclaimed == 1, "the released stale row must be reclaimed"
            with closing(sqlite3.connect(tmp_path / "spills.db")) as conn:
                remaining = {
                    request_id for (request_id,) in conn.execute("SELECT request_id FROM crawl_spills")
                }
            assert remaining == {"r-live"}, "the fenced live crawl's row must survive the sweep"
        finally:
            store.close()

    async def test_the_tick_rides_the_store_gc_twin_off_the_loop(self, tmp_path, monkeypatch):
        from crawlutron.api import spill_sweep
        from crawlutron.storage.result_store import CrawlSpillStore
        from tests._result_builders import engine_result

        store = CrawlSpillStore(tmp_path / "spills.db")
        try:
            store.put("r1", 0, engine_result("https://spill.test/0", "rendered"))
            seen: list[float] = []

            async def fake_twin(_self, max_age_seconds):
                seen.append(max_age_seconds)
                return 0

            monkeypatch.setattr(CrawlSpillStore, "agc_stale_spills", fake_twin)
            await spill_sweep.spill_maintenance_once(store, max_age_seconds=3600.0)
            assert seen == [3600.0], "the tick must run the store's async GC twin, not the sync method"
        finally:
            store.close()


class TestSpillSweepLoop:
    async def test_a_raising_tick_does_not_end_the_loop(self, monkeypatch, caplog):
        from crawlutron.api import spill_sweep

        calls: list[int] = []
        stop = asyncio.Event()

        async def _tick(store, *, max_age_seconds):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("gc exploded")
            stop.set()

        monkeypatch.setattr(spill_sweep, "spill_maintenance_once", _tick)

        with caplog.at_level("ERROR", logger="crawlutron.api.spill_sweep"):
            await asyncio.wait_for(
                spill_sweep.sweep_spills_forever(store=None, shutdown_event=stop, interval_seconds=0.0),
                timeout=30,
            )

        assert len(calls) == 2, "the loop must survive a raising tick and run the next one"
        assert any("spill_maintenance_pass_failed" in r.message for r in caplog.records)


class TestSpillSweepWiring:
    async def test_the_lifespan_starts_and_drains_the_spill_sweep_task(self, monkeypatch, tmp_path):
        from crawlutron import main as main_mod

        monkeypatch.setenv("CRAWLUTRON_CACHE_DIR", str(tmp_path))
        stub_heavy_teardown(monkeypatch)
        app = main_mod.create_app()

        async with app.router.lifespan_context(app):
            task = app.state.spill_maintenance_task
            assert task is not None, "startup must start the spill sweep task"
            assert not task.done()

        assert task.done(), "shutdown must drain the spill sweep task"

    async def test_the_spill_sweep_sweeps_the_shared_spill_store(self, monkeypatch, tmp_path):
        from crawlutron import main as main_mod
        from crawlutron.api import spill_sweep

        monkeypatch.setenv("CRAWLUTRON_CACHE_DIR", str(tmp_path))
        stub_heavy_teardown(monkeypatch)
        app = main_mod.create_app()
        swept: list[object] = []
        real_sweep = spill_sweep.sweep_spills_forever

        async def recording_sweep(store, *, shutdown_event, **kwargs):
            swept.append(store)
            return await real_sweep(store, shutdown_event=shutdown_event, **kwargs)

        monkeypatch.setattr(spill_sweep, "sweep_spills_forever", recording_sweep)

        async with app.router.lifespan_context(app):
            pass

        assert swept and swept[0] is app.state.spill_store, (
            "the sweep must run against the shared spill store production writes"
        )
