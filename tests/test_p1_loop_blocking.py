"""P1 defect tier: blocking disk I/O, serialization and DNS held on the
asyncio event loop, or run under a ``threading.Lock`` the loop contends on.

Every test asserts an observable outcome — a concurrent lock holder or the
event loop itself keeps making progress while the blocking work is in flight —
rather than which internal helper happened to run. The blocking stubs spin on
a counter only the event loop can advance, so the same work run on the loop
fails on the observed count instead of on a wall-clock guess.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path

import pytest

import app.crawl.content_cache as cc
from app.api.job_runner import _run_job
from app.api_models import CrawlPolicy
from app.crawl.content_cache import ContentCache
from app.crawl.engine import _ResultSpill
from app.crawl.job_store import JobStatus
from tests._fetcher_factories import crawled_results
from tests._job_harnesses import make_job_store
from tests._timing_harnesses import HANG_GUARD_SECONDS


class _LoopLiveness:
    """A counter only the event loop can advance.

    The heartbeat runs on the loop and bumps the count under a
    ``threading.Condition``; the blocking work waits on that condition and
    returns how far the loop got. The same work run *on* the loop can never
    tick it, so it observes zero progress and fails on the count.

    This was a wall-clock race before: the heartbeat slept 0.01s a hundred
    times to accumulate ticks while the worker busy-spun on ``time.sleep``.
    The condition removes the race entirely -- the worker is woken BY the tick
    it was waiting for, so there is no rate at which the loop has to tick for
    the assertion to hold, and no timeout in the success path at all.
    """

    def __init__(self, needed: int = 3) -> None:
        self.needed = needed
        self.ticks = 0
        self._condition = threading.Condition()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(0)
            with self._condition:
                self.ticks += 1
                self._condition.notify_all()

    def wait_for_progress(self, timeout: float = HANG_GUARD_SECONDS) -> int:
        """Blocking worker: return how far the loop advanced while we waited.

        The baseline is snapshotted on entry, so the count can only be
        satisfied by ticks that land WHILE the blocking work is in flight --
        the same work run on the loop observes zero and fails on the count.
        """
        with self._condition:
            baseline = self.ticks
            self._condition.wait_for(lambda: self.ticks - baseline >= self.needed, timeout=timeout)
            return self.ticks - baseline

    async def run_with_heartbeat(self, work, *, timeout: float):
        """Await ``work()`` with the heartbeat ticking; cancel the tick task.

        The shared scenario scaffolding — spawn the heartbeat task, bound the
        run, and cancel the heartbeat no matter how the work exits —
        exists here and nowhere else, so a test cannot forget the ``finally``
        and leak the heartbeat into the next test's tick count.
        """
        hb = asyncio.ensure_future(self.heartbeat())
        try:
            return await asyncio.wait_for(work(), timeout=timeout)
        finally:
            hb.cancel()


async def _wait_for(flag: threading.Event, *, reason: str) -> None:
    """Block until a worker thread sets ``flag``.

    ``flag`` is a ``threading.Event`` because it is set from a worker thread,
    and the wait runs in a thread too -- so the event loop stays free to run
    whatever it is the loop's job to run. A poll loop here would hold the GIL
    for exactly as long as it is waiting on the thread that wants it.
    """
    if not await asyncio.to_thread(flag.wait, HANG_GUARD_SECONDS):
        pytest.fail(f"{reason} (no signal within {HANG_GUARD_SECONDS}s)")


def _serve_stale_envelope(monkeypatch, cache: ContentCache, stale: bytes) -> list[bool]:
    """Serve ``stale`` bytes for key "k"'s envelope file until the toggle clears.

    The envelope read runs off the lock and can race a concurrent refresh, so
    these tests must control exactly what the file read returns while the
    assertion runs; the returned toggle re-enables the real bytes once the
    racy window is done. The staging (capture the real ``read_bytes``, patch
    the conditional stub) exists here and nowhere else.
    """
    serve = [True]
    real_read_bytes = Path.read_bytes

    def fake_read(path, *args, **kwargs):
        if serve[0] and path == cache._path_for("k"):
            return stale
        return real_read_bytes(path, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "read_bytes", fake_read)
    return serve


def _clocked_cache(tmp_path) -> tuple[list[float], ContentCache]:
    """A cache whose clock the test advances; return ``(now, cache)``.

    The tests here are about a verdict computed at one instant from bytes read
    at another, so the clock has to be a value they move. One builder keeps the
    ttl/byte-cap pair they share from being restated as two literals.
    """
    now = [1000.0]
    cache = ContentCache(str(tmp_path), default_ttl=30, clock=lambda: now[0], max_entry_bytes=10_000)
    return now, cache


class TestContentCacheDiskIOOffTheLock:
    """``ContentCache`` advertises that disk I/O never holds ``self._lock``;
    the event-loop thread contends on that lock (``aget``/``aset``), so a
    multi-MB read/parse, a prune scan, or a clear must run outside it."""

    def _guard_disk_calls(self, monkeypatch, cache: ContentCache) -> list[str]:
        """Record any cache-file syscall that runs while the lock is held."""
        offenders: list[str] = []

        def in_cache_dir(path: Path) -> bool:
            if path == cache.cache_dir:
                return True
            return path.parent == cache.cache_dir and not path.name.startswith(".")

        def guard(name: str, fn):
            def wrapper(self, *args, **kwargs):
                if in_cache_dir(self) and cache._lock.locked():
                    offenders.append(name)
                return fn(self, *args, **kwargs)

            return wrapper

        # read_text/read_bytes both: _read_envelope is the single chokepoint for
        # full reads, and the guard must hold whichever syscall it uses.
        monkeypatch.setattr(cc.Path, "read_text", guard("read_text", cc.Path.read_text))
        monkeypatch.setattr(cc.Path, "read_bytes", guard("read_bytes", cc.Path.read_bytes))
        monkeypatch.setattr(cc.Path, "stat", guard("stat", cc.Path.stat))
        monkeypatch.setattr(cc.Path, "exists", guard("exists", cc.Path.exists))
        monkeypatch.setattr(cc.Path, "unlink", guard("unlink", cc.Path.unlink))
        monkeypatch.setattr(cc.Path, "glob", guard("glob", cc.Path.glob))
        return offenders

    async def test_envelope_read_does_not_hold_the_lock(self, monkeypatch, tmp_path) -> None:
        cache = ContentCache(
            str(tmp_path),
            default_ttl=0,
            max_entry_bytes=8 * 1024 * 1024,
            max_total_bytes=64 * 1024 * 1024,
        )
        await cache.aset("big", {"body": "x" * (4 * 1024 * 1024)})
        cache._memory_cache.clear()  # force the on-disk envelope read

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
            # A loop-side lock holder (aset's index commit) must be able to
            # finish while the envelope read is still in flight. Yield the loop
            # until it has, rather than sleeping a hopeful tenth of a second and
            # asking whether it had: the writer either finished or it did not,
            # and the read is held open for the whole window either way.
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

    def test_prune_does_no_disk_io_under_the_lock(self, monkeypatch, tmp_path) -> None:
        now = [1000.0]
        cache = ContentCache(str(tmp_path), default_ttl=10, clock=lambda: now[0], max_entry_bytes=10_000)
        cache.set("a", {"body": "a" * 200})
        cache.set("b", {"body": "b" * 200})
        now[0] = 2000.0  # both entries are past their TTL

        offenders = self._guard_disk_calls(monkeypatch, cache)
        assert cache.prune() == 2
        assert not offenders, f"disk I/O ran while holding the cache lock: {offenders}"
        assert cache.get("a") is None and cache.get("b") is None

    def test_prune_repairs_sizes_without_statting_under_the_lock(self, monkeypatch, tmp_path) -> None:
        cache = ContentCache(str(tmp_path), default_ttl=60, max_entry_bytes=10_000)
        cache.set("k", {"body": "v" * 300}, ttl=60)
        cache._index_sizes.clear()  # crash residue: the recorded size was lost
        offenders = self._guard_disk_calls(monkeypatch, cache)
        assert cache.prune() == 0
        assert not offenders, f"disk I/O ran while holding the cache lock: {offenders}"
        path = cache._index["k"]
        assert cache._index_sizes["k"] == path.stat().st_size

    def test_prune_does_not_destroy_an_entry_refreshed_during_the_scan(self, monkeypatch, tmp_path) -> None:
        """The expiry verdict was computed from bytes read off the lock, so a
        re-set that landed during the scan (same deterministic path, a new TTL)
        must not be applied: the entry is live, and unlinking it destroys data.

        The direct analogue of
        :meth:`test_read_of_superseded_bytes_does_not_enter_the_mirror` for the
        prune verdict: the mirror guard there and the verdict guard here are the
        same currency check against the recorded index entry.
        """
        now, cache = _clocked_cache(tmp_path)
        cache.set("k", {"body": "a" * 100}, ttl=10)  # expires 1010
        # The refresh a concurrent writer landed during the scan: a longer
        # payload so the recorded byte size differs, and a TTL still live.
        now[0] = 1005.0
        cache.set("k", {"body": "b" * 200}, ttl=900)  # expires 1905
        now[0] = 1020.0  # the OLD envelope is expired, the new one is live

        # prune's glob reads the bytes as they were just before the refresh:
        # an expired verdict computed from superseded bytes.
        stale = json.dumps({"expires": 1010.0, "value": {"body": "a" * 100}, "cached_at": 1000.0}).encode()
        assert len(stale) != cache._index_sizes["k"], "the refresh must change the byte size"
        serve_stale = _serve_stale_envelope(monkeypatch, cache, stale)
        assert cache.prune() == 0, "a live entry refreshed during the scan was unlinked"
        assert "k" in cache._index, "the live entry was expunged from the index"
        # The live entry must still be servable from the bytes actually on disk.
        serve_stale[0] = False
        cache._memory_cache.clear()  # force the on-disk envelope read
        assert cache.get("k") == {"body": "b" * 200}

    def test_stale_read_does_not_poison_the_freshness_mirror(self, monkeypatch, tmp_path) -> None:
        """The freshness mirror is written from bytes read off the lock, so a
        superseded read must not record its ``(expires, cached_at)``: a
        fabricated timestamp and a stale ``expires=None`` (which makes a live
        entry look never-expiring) both violate the module contract that the
        fast path can never answer from metadata the slow path would have
        expired.

        The sibling value-mirror write in the same method is already guarded;
        this is the same guard for the metadata mirror.
        """
        now, cache = _clocked_cache(tmp_path)
        cache.set("k", {"v": "old"})  # (expires, cached_at) = (1030.0, 1000.0)
        real = cache._index_cached_at["k"]
        assert real == (1030.0, 1000.0)
        cache._memory_cache.clear()
        cache._index_cached_at.clear()  # force the metadata to be re-read from disk

        # Bytes that no longer match the recorded entry: a write landed between
        # the read and the index commit. The stale envelope claims to never
        # expire and to have been cached at 1.0.
        stale = json.dumps({"expires": None, "value": {"v": "stale"}, "cached_at": 1.0}).encode()
        assert len(stale) != cache._index_sizes["k"]
        serve_stale = _serve_stale_envelope(monkeypatch, cache, stale)
        assert cache.cached_at_of("k") == 1.0  # served the bytes that were read
        # but the superseded read must not enter the freshness mirror: a
        # fabricated cached_at=1.0 and a stale expires=None (which makes a live
        # entry look never-expiring) both answer later lookups from metadata the
        # slow path would have expired -- the mirror contract the value-mirror
        # write already enforces.
        assert "k" not in cache._index_cached_at, (
            f"a superseded read poisoned the freshness mirror: {cache._index_cached_at.get('k')!r}"
        )
        # The real bytes still populate the mirror with the real metadata.
        serve_stale[0] = False
        assert cache.cached_at_of("k") == 1000.0
        assert cache._index_cached_at["k"] == real

    def test_clear_does_no_disk_io_under_the_lock(self, monkeypatch, tmp_path) -> None:
        cache = ContentCache(str(tmp_path), default_ttl=0)
        cache.set("a", {"v": 1})
        offenders = self._guard_disk_calls(monkeypatch, cache)
        assert cache.clear() == 1
        assert not offenders, f"disk I/O ran while holding the cache lock: {offenders}"
        assert cache.get("a") is None


class TestJobRunnerSavesOffTheEventLoop:
    """``_run_job`` calls ``JobStore.save`` twice per batch; the JSON
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
            # The blocking work JobStore.save does: json.dumps of every row
            # field plus the SQLite commit. Only the event loop can advance
            # liveness, so a save run ON the loop sees zero progress.
            observed[0] = liveness.wait_for_progress()
            real_save(j)

        monkeypatch.setattr(store, "save", slow_save)
        shutdown = asyncio.Event()
        shutdown.set()  # exit at the first batch boundary, before any crawl

        async def scenario() -> int:
            runner = asyncio.ensure_future(_run_job(job.job_id, store, None, shutdown))
            await asyncio.wait_for(runner, timeout=15)
            return observed[0]

        progress = await liveness.run_with_heartbeat(scenario, timeout=20)
        assert progress >= liveness.needed, (
            f"the event loop made {progress} heartbeat ticks while a job-store save ran"
        )
        # H3 two-plane contract: the data save ran (recorded below) off the
        # loop, and the shutdown pause is a pure CONTROL write -- a CAS,
        # observed on the row, never carried by save() anymore.
        assert saved, "the runner's write-ahead data save never ran"
        assert store.load(job.job_id).status is JobStatus.PAUSED


class TestAsyncCrawlRunnerSavesOffTheEventLoop:
    """``run_async_crawl`` must not serialize+commit the job row on the loop.

    ``_run_job`` (the whole-site runner above) already offloads its saves via
    ``asyncio.to_thread(job_store.save, ...)``. The async single-shot runner
    called ``job_store.save`` directly at its RUNNING / COMPLETED / FAILED
    transitions -- same store, contradictory discipline, and the full response
    body was serialized and written while the loop stalled. The blind spot
    survived because the guard above drives only ``_run_job``; this one drives
    ``run_async_crawl`` itself, so the async path cannot regress unnoticed
    again.
    """

    async def test_run_async_crawl_save_does_not_block_the_event_loop(self, monkeypatch, tmp_path) -> None:
        from app.api.async_crawl_runner import run_async_crawl
        from app.crawl.result_store import CrawlResultStore
        from app.settings import CrawlerSettings
        from tests._job_harnesses import make_job_store

        store = make_job_store(str(tmp_path))
        job = store.create_single_shot(
            "crawl_loop_test", "http://example.com/", {"url": "http://example.com/"}
        )

        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the blocking save
        real_save = store.save

        def slow_save(j):
            # The blocking work JobStore.save does: json.dumps of every row
            # field plus the SQLite commit. Only the event loop can advance
            # liveness, so a save run ON the loop sees zero progress.
            observed[0] = max(observed[0], liveness.wait_for_progress())
            real_save(j)

        # ``asave`` delegates to ``save`` off-thread, so patching the sync
        # method covers every save the runner makes.
        monkeypatch.setattr(store, "save", slow_save)

        async def _fake_execute(*args, **kwargs):
            # A completed crawl drives the COMPLETED save too, not just the
            # RUNNING one, and avoids any real network work.
            return {"results": [], "submitted_count": 0}

        monkeypatch.setattr("app.api.async_crawl_runner.execute_crawl", _fake_execute)

        async def scenario() -> int:
            await asyncio.wait_for(
                run_async_crawl(
                    job.job_id,
                    {"url": "http://example.com/"},
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
        # The job reached COMPLETED through the offloaded save, so the store is
        # not just skipping the work.
        assert store.load(job.job_id).status == JobStatus.COMPLETED


class TestSpillAssemblyOffTheEventLoop:
    """``run_crawl_request`` reassembles the spilled results with
    ``load_ordered``: a full file scan plus a ``json.loads`` per line. That is
    blocking work and must not run on the event loop."""

    async def test_load_ordered_does_not_block_the_event_loop(self, monkeypatch) -> None:
        liveness = _LoopLiveness()
        observed: list[int] = [-1]  # loop progress seen by the blocking read-back
        real_load = _ResultSpill.load_ordered

        def slow_load(self):
            observed[0] = liveness.wait_for_progress()
            return real_load(self)

        monkeypatch.setattr(_ResultSpill, "load_ordered", slow_load)

        pages = {"/": (200, "<html><body>hello</body></html>", "text/html")}

        async def scenario() -> dict:
            # The crawl is scheduled, not awaited inline: the point is what the
            # event loop does while the spilled results are read back.
            crawl = asyncio.ensure_future(crawled_results(pages, limits={"max_depth": 1, "max_pages": 5}))
            return await asyncio.wait_for(crawl, timeout=15)

        out = await liveness.run_with_heartbeat(scenario, timeout=20)
        assert observed[0] >= liveness.needed, (
            f"the event loop made {observed[0]} heartbeat ticks while the spilled results were read back"
        )
        assert len(out["results"]) == 1
        assert out["results"][0]["original_url"] == "http://example.com/"

    def test_read_of_superseded_bytes_does_not_enter_the_mirror(self, monkeypatch, tmp_path) -> None:
        """The envelope read runs off the lock, so only a read that still
        matches the recorded entry may warm the RAM mirror: otherwise a read
        overlapping a concurrent write would shadow the newer value."""
        cache = ContentCache(str(tmp_path), default_ttl=0, max_entry_bytes=10_000)
        cache.set("k", {"v": "old"})
        cache._memory_cache.clear()

        # Bytes that no longer match the recorded entry: a write landed between
        # the read and the index commit.
        stale = json.dumps({"expires": None, "value": {"v": "stale"}, "cached_at": 1.0}).encode()
        assert len(stale) != cache._index_sizes["k"]
        _serve_stale_envelope(monkeypatch, cache, stale)  # toggle stays on: whole read is stale
        assert cache.get("k") == {"v": "stale"}  # served the bytes that were read
        assert "k" not in cache._memory_cache  # but the stale read is not mirrored
