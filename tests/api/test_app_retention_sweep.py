"""App-lifespan retention sweep: shared stores reclaimed on the tick, a raising
tick never ends the loop, and the lifespan starts and drains the sweep task.
"""

from __future__ import annotations

import asyncio

from crawlutron.service import retention_sweep
from tests._app_factory import wired_app
from tests._retention_sweep_support import (
    assert_lifespan_sweep_task,
    stores,
    sweep_reclaims_tombstone_and_expired_result,
)


class TestRetentionSweepTick:
    def test_the_tick_reclaims_a_past_grace_tombstone_and_an_expired_result(self, tmp_path):
        sweep_reclaims_tombstone_and_expired_result(tmp_path)


class TestRetentionSweepLoop:
    async def test_a_raising_tick_does_not_end_the_loop(self, monkeypatch, caplog, tmp_path):
        job_store, result_store = stores(tmp_path)
        try:
            calls: list[int] = []
            stop = asyncio.Event()

            def _tick(*args, **kwargs):
                calls.append(1)
                if len(calls) == 1:
                    raise RuntimeError("sweep exploded")
                stop.set()

            monkeypatch.setattr(retention_sweep, "retention_maintenance_once", _tick)

            with caplog.at_level("ERROR", logger="crawlutron.service.retention_sweep"):
                await asyncio.wait_for(
                    retention_sweep.sweep_retention_forever(
                        job_store,
                        result_store,
                        shutdown_event=stop,
                        interval_seconds=0.0,
                    ),
                    timeout=30,
                )

            assert len(calls) == 2, "the loop must survive a raising tick and run the next one"
            assert any("retention_maintenance_pass_failed" in r.message for r in caplog.records)
        finally:
            job_store.close()
            result_store.close()


class TestRetentionSweepWiring:
    def _wired_app(self, monkeypatch, tmp_path):
        return wired_app(monkeypatch, tmp_path)

    async def test_the_lifespan_starts_and_drains_the_retention_sweep_task(self, monkeypatch, tmp_path):
        app = self._wired_app(monkeypatch, tmp_path)
        await assert_lifespan_sweep_task(app, "retention_sweep_task", "retention sweep")

    async def test_the_retention_sweep_sweeps_the_shared_job_store(self, monkeypatch, tmp_path):
        from crawlutron.service import retention_sweep

        app = self._wired_app(monkeypatch, tmp_path)
        swept: list[tuple[object, object]] = []
        real_sweep = retention_sweep.sweep_retention_forever

        async def recording_sweep(job_store, result_store, shutdown_event, **kwargs):
            swept.append((job_store, result_store))
            return await real_sweep(job_store, result_store, shutdown_event=shutdown_event, **kwargs)

        monkeypatch.setattr(retention_sweep, "sweep_retention_forever", recording_sweep)

        async with app.router.lifespan_context(app):
            await asyncio.sleep(0)
            app.state.shutdown_event.set()
            await asyncio.wait_for(app.state.retention_sweep_task, timeout=10)

        assert swept and swept[0][0] is app.state.job_store, (
            "the sweep must run against the shared job store production writes to"
        )
        assert swept[0][1] is app.state.result_store, (
            "the sweep must run against the shared result store production writes to"
        )
