"""Blocking disk IO, serialization and DNS held off the event loop.

Every test asserts an observable outcome -- a concurrent holder or the loop
itself keeps advancing -- while the blocking work is in flight."""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

import crawlutron.transport.content_cache as cc
from crawlutron.api.job_runner import run_job
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.storage.job_store import JobStatus
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.content_cache import ContentCache
from tests._cache_fixtures import (
    counting_entry_reads,
    entry_path,
    on_disk_bytes,
    parked_entry_unlink,
)
from tests._clocks import HANG_GUARD_SECONDS, clocked_cache
from tests._fetcher_factories import crawled_results
from tests._job_harnesses import loaded_job, make_job_store
from tests._loop_liveness import LoopLiveness as _LoopLiveness


async def _wait_for(flag: threading.Event, *, reason: str) -> None:
    """Block until a worker thread sets ``flag``.

    The wait runs in a thread, so the loop stays free; a poll loop here would
    hold the GIL for as long as it waits on the thread that wants it."""
    if not await asyncio.to_thread(flag.wait, HANG_GUARD_SECONDS):
        pytest.fail(f"{reason} (no signal within {HANG_GUARD_SECONDS}s)")


def _serve_stale_envelope(monkeypatch, cache: ContentCache, stale: bytes) -> list[bool]:
    """Serve ``stale`` bytes for key "k"'s envelope file until the toggle clears.

    The envelope read runs off the lock and can race a refresh; the returned
    toggle re-enables the real bytes once the racy window is done."""
    serve = [True]
    real_read_bytes = Path.read_bytes

    def fake_read(path, *args, **kwargs):
        if serve[0] and path == entry_path(cache, "k"):
            return stale
        return real_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "read_bytes", fake_read)
    return serve


def _cache_on_movable_time(tmp_path) -> tuple[list[float], ContentCache]:
    """A cache whose clock the test advances; return ``(now, cache)``.

    These tests pin a verdict computed at one instant from bytes read at
    another; one builder keeps the shared ttl/byte-cap pair stated once."""
    cache, now = clocked_cache(str(tmp_path), default_ttl=30, max_entries=10_000)
    return now, cache


class TestContentCacheDiskIOOffTheLock:
    """``ContentCache`` advertises that disk I/O never holds ``self._lock``;
    the event-loop thread contends on that lock (``aget``/``aset``), so a
    multi-MB read/parse, a prune scan, or a clear must run outside it."""

    async def _prove_off_lock(self, monkeypatch, cache, op, expected) -> None:
        """Run ``op`` off-loop and prove the loop stays live while its unlink
        is parked: disk I/O never holds the commit lock."""
        with parked_entry_unlink(monkeypatch, cache) as (started, release):
            task = asyncio.ensure_future(asyncio.to_thread(op))
            await asyncio.to_thread(started.wait, 10)  # the op reached its unlink
            # The loop stays responsive while the op's unlink is parked.
            await asyncio.wait_for(cache.aset("live", {"body": "v"}), timeout=2)
            assert await cache.aget("live") == {"body": "v"}
            release.set()
            assert await asyncio.wait_for(task, timeout=10) == expected

    async def test_envelope_read_does_not_hold_the_lock(self, monkeypatch, tmp_path) -> None:
        cache = ContentCache(
            str(tmp_path),
            default_ttl=60,
            max_entry_bytes=8 * 1024 * 1024,
            max_total_bytes=64 * 1024 * 1024,
        )
        await cache.aset(
            "big", {"body": "x" * (4 * 1024 * 1024)}
        )  # past the mirror size gate: always on-disk

        release = threading.Event()
        read_started = threading.Event()
        real_read_text = Path.read_text
        real_read_bytes = Path.read_bytes

        def blocking_read(real):
            """One wrapper for both read paths: the block is about the file, not the call."""

            def read(self, *args, **kwargs):
                if self.parent == cache.cache_dir and not self.name.startswith("."):
                    read_started.set()
                    release.wait(timeout=10)
                return real(self, *args, **kwargs)

            return read

        slow_read = blocking_read(real_read_text)
        slow_read_bytes = blocking_read(real_read_bytes)

        monkeypatch.setattr(cc.Path, "read_text", slow_read)
        monkeypatch.setattr(cc.Path, "read_bytes", slow_read_bytes)

        async def scenario() -> bool:
            reading = asyncio.ensure_future(cache.aget("big"))
            await _wait_for(read_started, reason="the envelope read never started")
            # A loop-side lock holder (aset's index commit) must be able to finish while the envelope read is
            # still in flight.
            writer = asyncio.ensure_future(cache.aset("other", {"v": 1}))
            try:
                await asyncio.wait_for(asyncio.shield(writer), timeout=1.0)
            except TimeoutError:
                writer_finished_while_reading = False
            else:
                writer_finished_while_reading = True
            release.set()
            await asyncio.wait_for(reading, timeout=5)
            await asyncio.wait_for(writer, timeout=5)
            return writer_finished_while_reading

        assert await asyncio.wait_for(scenario(), timeout=15), (
            "a concurrent lock holder was blocked for the whole envelope read"
        )

    async def test_prune_does_no_disk_io_under_the_lock(self, monkeypatch, tmp_path) -> None:
        now = [1000.0]
        cache = ContentCache(str(tmp_path), default_ttl=10, clock=lambda: now[0], max_entry_bytes=10_000)
        cache.set("a", {"body": "a" * 200})
        cache.set("b", {"body": "b" * 200})
        now[0] = 2000.0  # both entries are past their TTL

        await self._prove_off_lock(monkeypatch, cache, cache.prune, 2)
        assert cache.get("a") is None and cache.get("b") is None

    def test_prune_keeps_byte_accounting_consistent_without_statting_under_lock(
        self, monkeypatch, tmp_path
    ) -> None:
        cache = ContentCache(str(tmp_path), default_ttl=60, max_entry_bytes=10_000)
        cache.set("k", {"body": "v" * 300}, ttl=60)
        assert cache.prune() == 0
        # Accounted bytes match the surviving on-disk file exactly, so the
        # eviction budget never drifts from the real footprint.
        assert on_disk_bytes(cache) == entry_path(cache, "k").stat().st_size

    def test_prune_does_not_destroy_an_entry_refreshed_during_the_scan(self, monkeypatch, tmp_path) -> None:
        """The expiry verdict came from bytes read off the lock; a re-set that
        landed during the scan must not be applied -- the entry is live and
        unlinking destroys data. The mirror-guard analogue for the prune verdict."""
        now, cache = _cache_on_movable_time(tmp_path)
        cache.set("k", {"body": "a" * 100}, ttl=10)  # expires 1010
        # The refresh a concurrent writer landed during the scan: a longer payload so the recorded byte size
        # differs, and a TTL still live.
        now[0] = 1005.0
        cache.set("k", {"body": "b" * 200}, ttl=900)  # expires 1905
        now[0] = 1020.0  # the OLD envelope is expired, the new one is live

        # Prune's glob reads the bytes as they were just before the refresh: an expired verdict computed from
        # superseded bytes.
        stale = json.dumps({"expires": 1010.0, "value": {"body": "a" * 100}, "cached_at": 1000.0}).encode()
        assert len(stale) != entry_path(cache, "k").stat().st_size, "the refresh must change the byte size"
        serve_stale = _serve_stale_envelope(monkeypatch, cache, stale)
        assert cache.prune() == 0, "a live entry refreshed during the scan was unlinked"
        # The live entry must still be servable from the bytes actually on disk.
        serve_stale[0] = False
        assert cache.get("k") == {"body": "b" * 200}

    def test_stale_read_does_not_poison_the_freshness_mirror(self, monkeypatch, tmp_path) -> None:
        """A live entry's ``cached_at`` must be answered from its real metadata,
        never a superseded read's value."""
        _, cache = _cache_on_movable_time(tmp_path)
        cache.set("k", {"v": "old"})  # (expires, cached_at) = (1030.0, 1000.0)
        assert cache.cached_at_of("k") == 1000.0

        # The superseded read must not displace the live entry's metadata: the
        # answer stays the real cached_at while the entry is live.
        stale = json.dumps({"expires": None, "value": {"v": "stale"}, "cached_at": 1.0}).encode()
        serve_stale = _serve_stale_envelope(monkeypatch, cache, stale)
        # A live-entry cached-at answer is served from the mirror, so it never
        # reflects the superseded disk value; it reads the real metadata.
        assert cache.cached_at_of("k") == 1000.0
        serve_stale[0] = False

    async def test_clear_does_no_disk_io_under_the_lock(self, monkeypatch, tmp_path) -> None:
        cache = ContentCache(str(tmp_path), default_ttl=60)
        cache.set("a", {"v": 1})
        await self._prove_off_lock(monkeypatch, cache, cache.clear, 1)
        assert cache.get("a") is None


class TestJobRunnerSavesOffTheEventLoop:
    """``run_job`` calls ``JobStore.save`` twice per batch; the JSON
    serialization of the whole row and the SQLite commit are blocking work and
    must not run on the event loop."""

    async def test_save_does_not_block_the_event_loop(self, monkeypatch, tmp_path) -> None:
        store = make_job_store(str(tmp_path))
        job = store.create("http://example.com/", CrawlPolicy())

        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the blocking save
        saved: list[JobStatus] = []
        real_save = store.save

        def slow_save(j):
            saved.append(j.status)
            # The blocking work JobStore.save does: json.dumps of every row field plus the SQLite commit. Only
            # the event loop can advance liveness, so a save run ON the loop sees zero progress.
            observed[0] = liveness.wait_for_progress()
            real_save(j)

        monkeypatch.setattr(store, "save", slow_save)
        shutdown = asyncio.Event()
        shutdown.set()  # exit at the first batch boundary, before any crawl

        async def scenario() -> int:
            runner = asyncio.ensure_future(
                run_job(job.job_id, store, CrawlerSettings.from_mapping({}), shutdown)
            )
            await asyncio.wait_for(runner, timeout=15)
            return observed[0]

        progress = await liveness.run_with_heartbeat(scenario, timeout=20)
        assert progress >= liveness.needed, (
            f"the event loop made {progress} heartbeat ticks while a job-store save ran"
        )
        # Two-plane contract: the data save ran (recorded below) off the loop, and the shutdown pause is a
        # pure CONTROL write -- a CAS, observed on the row, never carried by save() anymore.
        assert saved, "the runner's write-ahead data save never ran"
        assert loaded_job(store, job.job_id).status is JobStatus.PAUSED


class TestAsyncCrawlRunnerSavesOffTheEventLoop:
    """``run_async_crawl`` must not serialize+commit the job row on the loop: the
    single-shot runner once called ``job_store.save`` directly at its transitions,
    stalling the loop -- this drives the runner itself, not just the whole-site one.
    """

    async def test_run_async_crawl_save_does_not_block_the_event_loop(self, monkeypatch, tmp_path) -> None:
        from crawlutron.api.async_crawl_runner import run_async_crawl
        from crawlutron.config.settings import CrawlerSettings
        from crawlutron.storage.result_store import CrawlResultStore
        from tests._job_harnesses import make_job_store

        store = make_job_store(str(tmp_path))
        job = store.create_single_shot("crawl_loop_test", "http://example.com/", CrawlPolicy())

        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the blocking save
        real_save = store.save

        def slow_save(j):
            # The blocking work JobStore.save does: json.dumps of every row field plus the SQLite commit. Only
            # the event loop can advance liveness, so a save run ON the loop sees zero progress.
            observed[0] = max(observed[0], liveness.wait_for_progress())
            real_save(j)

        # ``asave`` delegates to ``save`` off-thread, so patching the sync method covers every save the runner
        # makes.
        monkeypatch.setattr(store, "save", slow_save)

        async def _fake_execute(*args, **kwargs):
            # A completed crawl drives the COMPLETED save too, not just the RUNNING one, and avoids any real
            # network work.
            return {"results": [], "submitted_count": 0}

        monkeypatch.setattr("crawlutron.api.async_crawl_runner.execute_crawl", _fake_execute)

        async def scenario() -> int:
            from crawlutron.models.crawl_request import CrawlRequest

            request = CrawlRequest(url="http://example.com/")
            await asyncio.wait_for(
                run_async_crawl(
                    job.job_id,
                    request,
                    None,
                    CrawlerSettings.from_mapping(),
                    store,
                    CrawlResultStore(tmp_path / "results.db"),
                    asyncio.Event(),
                ),
                timeout=20,
            )
            return observed[0]

        progress = await liveness.run_with_heartbeat(scenario, timeout=30)
        assert progress >= liveness.needed, (
            f"the event loop made {progress} heartbeat ticks while the async crawl runner saved a job row"
        )
        # The job reached COMPLETED through the offloaded save, so the store is not just skipping the work.
        assert loaded_job(store, job.job_id).status == JobStatus.COMPLETED


class TestSpillAssemblyOffTheEventLoop:
    """``run_crawl_request`` reassembles the spilled results through the
    store's ``aload_ordered`` twin: the blocking SELECT + decode runs on the
    store executor, never the event loop."""

    async def test_aload_ordered_does_not_block_the_event_loop(self, tmp_path, monkeypatch) -> None:
        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the blocking read-back
        real_load = CrawlSpillStore.load_ordered

        def slow_load(self, request_id, *, policy):
            observed[0] = liveness.wait_for_progress()
            return real_load(self, request_id, policy=policy)

        monkeypatch.setattr(CrawlSpillStore, "load_ordered", slow_load)

        pages = {"/": (200, "<html><body>hello</body></html>", "text/html")}
        store = CrawlSpillStore(tmp_path / "spills.db")
        try:

            async def scenario() -> ProducerEnvelope:
                # The crawl is scheduled, not awaited inline: the point is what the
                # event loop does while the spilled results are read back.
                crawl = asyncio.ensure_future(
                    crawled_results(pages, limits={"max_depth": 1, "max_pages": 5}, spill_store=store)
                )
                return await asyncio.wait_for(crawl, timeout=15)

            out = await liveness.run_with_heartbeat(scenario, timeout=20)
        finally:
            store.close()
        assert observed[0] >= liveness.needed, (
            f"the event loop made {observed[0]} heartbeat ticks while the spilled results were read back"
        )
        assert len(out["results"]) == 1
        assert out["results"][0].original_url == "http://example.com/"

    def test_read_of_superseded_bytes_does_not_enter_the_mirror(self, monkeypatch, tmp_path) -> None:
        """The envelope read runs off the lock, so only a read that still
        matches the recorded entry may warm the RAM mirror: otherwise a read
        overlapping a concurrent write would shadow the newer value."""
        cache = ContentCache(str(tmp_path), default_ttl=60, max_entry_bytes=10_000)
        cache.set("k", {"v": "old"})
        # Evict k from the RAM mirror (flood past MEMORY_CACHE_MAX) but leave it
        # registered in the disk index, so a read falls through to disk.
        for i in range(cc.MEMORY_CACHE_MAX + 1):
            cache.set(f"flood{i}", i)

        # Bytes that no longer match the recorded entry: a write landed between the read and the index commit.
        stale = json.dumps({"expires": None, "value": {"v": "stale"}, "cached_at": 1.0}).encode()
        assert len(stale) != entry_path(cache, "k").stat().st_size
        serve_stale = _serve_stale_envelope(monkeypatch, cache, stale)
        assert cache.get("k") == {"v": "stale"}  # served the bytes that were read
        serve_stale[0] = False
        # The superseded read must not have warmed the RAM mirror with stale
        # bytes: the next read goes back to disk for the real value.
        with counting_entry_reads(monkeypatch, cache, "k") as reads:
            assert cache.get("k") == {"v": "old"}
            assert reads == [1]

    async def test_a_failed_crawl_still_closes_its_private_spill_store(self, monkeypatch, tmp_path) -> None:
        """A raise in the crawl BODY must still close the crawl's private
        in-memory spill store; the caller-supplied store is never closed here
        (it outlives the crawl), so the private one is the close-on-error path
        the old file spill's close-on-error guard pinned (#221).
        """
        import crawlutron.orchestration.request_pipeline as pipeline

        closed: list[bool] = []
        real_close = CrawlSpillStore.close

        def _recording_close(self):
            closed.append(True)
            return real_close(self)

        monkeypatch.setattr(CrawlSpillStore, "close", _recording_close)

        async def _raise_crawl(**kwargs):
            raise RuntimeError("crawl body failed")

        monkeypatch.setattr(pipeline, "run_recursive_crawl", _raise_crawl)

        async def scenario():
            try:
                await crawled_results(
                    {"/": (200, "<html><body>hello</body></html>", "text/html")},
                    limits={"max_depth": 1, "max_pages": 5},
                )
            except RuntimeError:
                return
            raise AssertionError("the crawl-body error must propagate")

        await scenario()
        assert closed, "the private spill store was not closed when the crawl body raised"
