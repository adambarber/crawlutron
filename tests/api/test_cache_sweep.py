"""The app-lifespan cache maintenance pass: expired envelopes and orphan .tmp
files leave the shared cache dir on the tick, a raising prune never ends the
loop, and the lifespan starts and drains the maintenance task.
"""

import asyncio
import os
import pathlib

from tests._clocks import advance_clock, arrange_clock
from tests._lifespan_doubles import stub_heavy_teardown


class TestMaintenanceTick:
    async def test_the_tick_removes_an_expired_envelope_and_an_orphan_tmp(self, tmp_path):
        from crawlutron.api.cache_sweep import cache_maintenance_once
        from crawlutron.transport.content_cache import ContentCache

        now = 5000.0  # past TMP_ORPHAN_MAX_AGE_SECONDS so an mtime-0 orphan is aged out
        times, clock = arrange_clock(start=0.0)

        cache = ContentCache(tmp_path, clock=clock, max_total_bytes=None)
        cache.set("cold", {"v": 1}, ttl=10)
        expired_path = cache.cache_dir / f"{cache.namespace}.{cache.make_key('cold')}.json"
        orphan = tmp_path / f".{cache.namespace}.junk.tmp"
        orphan.write_text("{}")
        os.utime(orphan, (0, 0))

        advance_clock(times, now)
        removed = cache_maintenance_once(cache=cache, now=now)

        assert removed >= 2, "the expired envelope and the orphan .tmp must both go"
        assert not expired_path.exists()
        assert not orphan.exists()
        # A live entry written at the current time survives the same tick.
        hot_key = cache.make_key("hot")
        cache.set(hot_key, {"v": 2}, ttl=10)
        advance_clock(times, 1.0)
        cache_maintenance_once(cache=cache, now=now + 1)
        assert cache.get(hot_key) == {"v": 2}


class TestNamespaceCoverage:
    def test_production_constructs_exactly_one_content_cache(self):
        """Bypass attack: a second cache dir would be swept by nothing -- the
        maintenance pass prunes the shared instance only. Production must
        construct exactly one ContentCache (inside shared_content_cache), so
        the shared dir IS the only live dir."""
        import re

        root = pathlib.Path(__file__).resolve().parents[2] / "crawlutron"
        sites = [
            str(p.relative_to(root))
            for p in root.rglob("*.py")
            if re.search(r"ContentCache\(", p.read_text(encoding="utf-8"))
        ]
        assert sites == ["transport/content_cache.py"], (
            f"new ContentCache construction site(s) {sites}: the maintenance "
            "sweep prunes only the shared instance -- wire the new dir or share it"
        )


class TestSweepLoop:
    async def test_a_raising_tick_does_not_end_the_loop(self, monkeypatch, caplog):
        from crawlutron.api import cache_sweep

        calls: list[int] = []
        stop = asyncio.Event()

        def _tick(now=None, cache=None):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("prune exploded")
            stop.set()

        monkeypatch.setattr(cache_sweep, "cache_maintenance_once", _tick)

        with caplog.at_level("ERROR", logger="crawlutron.api.cache_sweep"):
            await asyncio.wait_for(
                cache_sweep.sweep_cache_forever(stop, interval_seconds=0.0),
                timeout=30,
            )

        assert len(calls) == 2, "the loop must survive a raising tick and run the next one"
        assert any("cache_maintenance_pass_failed" in r.message for r in caplog.records)


class TestLifespanWiring:
    async def test_the_lifespan_starts_and_drains_the_maintenance_task(self, monkeypatch, tmp_path):
        from crawlutron import main as main_mod

        monkeypatch.setenv("CRAWLUTRON_CACHE_DIR", str(tmp_path))
        stub_heavy_teardown(monkeypatch)
        app = main_mod.create_app()

        async with app.router.lifespan_context(app):
            task = app.state.cache_maintenance_task
            assert task is not None, "startup must start the cache maintenance task"
            assert not task.done()

        assert task.done(), "shutdown must drain the cache maintenance task"
