"""Memory tier, entry lifecycle and unlink discipline hold."""

from __future__ import annotations

import shutil
import threading

import pytest

import app.transport.content_cache as cc
from tests._cache_fixtures import (
    counting_entry_reads,
    entry_replaced_by_directory,
    parked_entry_unlink,
    reopen_cache,
    scoped_cache,
    watch_writes,
)
from tests._fetcher_factories import make_seeded_cache


def _make(tmp_path, clock=1000.0):
    shutil.rmtree(tmp_path, ignore_errors=True)
    return cc.ContentCache(tmp_path, namespace="mem", clock=lambda: clock, max_entries=10000)


def _assert_concurrent_op_proceeds(monkeypatch, cache, worker, probe_key):
    """Park the worker's unlink and prove a concurrent get still completes."""
    with parked_entry_unlink(monkeypatch, cache) as (started, release):
        thread = threading.Thread(target=worker)
        thread.start()
        assert started.wait(10), "the worker never reached an unlink"
        result: list[object] = []
        probe = threading.Thread(target=lambda: result.append(cache.get(probe_key)))
        probe.start()
        probe.join(timeout=5)
        release.set()
        thread.join(timeout=10)
    assert result == [None], "a concurrent operation was blocked by the unlink"


def test_small_value_lives_in_memory(tmp_path, monkeypatch):
    c = _make(tmp_path)
    c.set("a", {"x": 1})
    assert c.get("a") == {"x": 1}
    # A RAM hit reads no bytes from disk.
    with counting_entry_reads(monkeypatch, c, "a") as reads:
        assert c.get("a") == {"x": 1}
        assert reads == [0]


def test_large_value_not_in_memory(tmp_path, monkeypatch):
    c = _make(tmp_path)
    big = "z" * (2 * 1024 * 1024)  # 2MB string -> shallow size exceeds 1MB
    c.set("big", big)
    # Not mirrored: a get falls through to one disk read and still answers.
    with counting_entry_reads(monkeypatch, c, "big") as reads:
        assert c.get("big") == big
        assert reads == [1]


def test_memory_ttl_honored(tmp_path):
    c = _make(tmp_path)
    c.set("k", "v", ttl=60)  # expires at 1060
    assert c.get("k") == "v"
    c.clock = lambda: 2000.0
    assert c.get("k") is None


def test_memory_lru_cap(tmp_path, monkeypatch):
    c = _make(tmp_path)
    for i in range(150):
        c.set(f"k{i}", i)
    # The RAM mirror holds at most MEMORY_CACHE_MAX: the NEWEST stays in RAM
    # (no disk read) while the OLDEST fell out (one disk read) but still answers.
    with counting_entry_reads(monkeypatch, c, "k149") as reads:
        assert c.get("k149") == 149
        assert reads == [0]
    with counting_entry_reads(monkeypatch, c, "k0") as reads:
        assert c.get("k0") == 0
        assert reads == [1]


def test_clear_clears_memory(tmp_path):
    c = _make(tmp_path)
    c.set("a", 1)
    assert c.get("a") == 1
    c.clear()
    assert c.get("a") is None


class TestContentCacheEntryLifecycle:
    """Corrupted entries, expiry, prune/clear and async disk writes"""

    def test_content_cache_get_expired(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1)
        clock[0] = 1002.0
        assert cache.get("key") is None

    @pytest.mark.parametrize("drop", ["prune", "clear"])
    def test_content_cache_drops_entries_and_reports_the_count(self, tmp_path, drop):
        """Whatever retired them, the entries are gone and the count said so.

        ``prune`` retires on TTL, ``clear`` unconditionally; one test over the
        two, since spelled per operation the ``clear`` copy lost the count.
        """
        cache, clock = make_seeded_cache(tmp_path, "key1", "key2", default_ttl=1)
        clock[0] = 1002.0
        assert getattr(cache, drop)() == 2
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    async def test_aset_aget_roundtrip(self):
        """Async disk cache stores and retrieves, including a large (HTML-sized)
        value that hits the on-disk path."""
        with scoped_cache(default_ttl=60) as cache:
            big = "<html>" + ("x" * 2_000_000) + "</html>"
            await cache.aset("k1", big)
            # RAM mirror only keeps <1MiB, so this exercises the disk read path.
            assert await cache.aget("k1") == big

    async def test_aset_writes_off_event_loop(self):
        """The disk write (json.dumps of a large value) runs in a worker thread."""
        with scoped_cache(default_ttl=60) as cache:
            seen: dict[str, threading.Thread] = {}
            watch_writes(cache, on_write=lambda path, data: seen.update(thread=threading.current_thread()))
            await cache.aset("k2", "y" * 2_000_000)
        assert seen, "write never happened"
        written_on = seen["thread"]
        assert written_on is not threading.current_thread()


class TestCachedAtReadsMetadataWithoutDeserializingTheValue:
    """Cached_at_of must not deserialize the whole envelope on every call.

    Freshness checks touch many keys per crawl; reading the value bytes off
    disk makes it O(bytes) instead of O(keys) -- the timestamp comes from a mirror.
    """

    def test_cached_at_after_set_reads_no_envelope(self, tmp_path):
        cache, _ = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"big": "x" * 100})
        cache.set("k", {"big": "y" * 100})  # ensure the mirror is refreshed

        # The freshness answer comes from the metadata mirror, not the value
        # bytes: swap the file for an unreadable dir and the answer still holds.
        with entry_replaced_by_directory(cache, "k"):
            assert cache.cached_at_of("k") is not None
            assert cache.cached_at_of("k") is not None

    def test_cached_at_value_matches_the_envelope(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"data": "value"})
        assert cache.cached_at_of("k") == clock[0]

    def test_cached_at_of_a_cold_on_disk_entry_answers_and_then_warms(self, tmp_path):
        """A warm restart (fresh instance, same directory) still answers
        correctly from disk, and the read pays for itself on later calls."""
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"data": "value"})
        cached_at = cache.cached_at_of("k")

        # The restarted instance reads the same time base, or the entry it finds is expired before it is ever
        # looked at.
        reopened = reopen_cache(tmp_path, default_ttl=60, clock=lambda: clock[0])
        # Cold mirror: one full read answers correctly.
        assert reopened.cached_at_of("k") == cached_at
        # Now warm: later freshness checks never touch the envelope, so even an
        # unreadable entry file doesn't lose the metadata answer.
        with entry_replaced_by_directory(reopened, "k"):
            assert reopened.cached_at_of("k") == cached_at

    def test_expired_entry_reports_none_through_the_chokepoint(self, tmp_path):
        """An expired mirror entry must still be expired and dropped by the
        shared chokepoint, not silently answered from stale metadata."""
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=10, value={"data": "value"})
        assert cache.cached_at_of("k") is not None
        clock[0] += 1000.0
        assert cache.cached_at_of("k") is None
        # The expired entry is gone, not just unanswered: a later read misses too.
        assert cache.get("k") is None

    def test_evicted_entry_is_not_answered_from_stale_metadata(self, tmp_path):
        with scoped_cache(default_ttl=60, clock=lambda: 1000.0, max_entries=1) as cache:
            cache.set("k1", {"data": "one"})
            assert cache.cached_at_of("k1") is not None
            # Evict k1 via the LRU cap.
            cache.set("k2", {"data": "two"})
            assert cache.cached_at_of("k1") is None
            assert cache.cached_at_of("k2") is not None


class TestContentCacheUnlinkDiscipline:
    def test_eviction_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """Eviction unlinks the doomed files outside the commit lock: a disk
        write is parked mid-unlink while a concurrent operation still runs."""
        cache = cc.ContentCache(tmp_path, max_entries=2, max_total_bytes=None, default_ttl=60)

        def evict():
            # Over-cap by one: the oldest is evicted and its file unlink parks.
            cache.set(cache.make_key("a"), {"v": 1})
            cache.set(cache.make_key("b"), {"v": 2})
            cache.set(cache.make_key("c"), {"v": 3})

        _assert_concurrent_op_proceeds(monkeypatch, cache, evict, cache.make_key("missing"))
        assert cache.get(cache.make_key("a")) is None
        assert cache.get(cache.make_key("c")) == {"v": 3}

    def test_expiry_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """The TTL-expiry path in _read_envelope unlinks outside the lock too."""
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1, value={"data": "value"})
        clock[0] = 1002.0  # the seeded entry is now expired

        def expire():
            # The read drops the now-expired key and parks on the unlink.
            cache.get("key")

        _assert_concurrent_op_proceeds(monkeypatch, cache, expire, "other-key")

    def test_re_set_key_survives_its_stale_eviction_unlink(self, tmp_path):
        """A key re-set after its eviction reuses the same deterministic path.

        The deferred unlink must spare a file that has become live again, or the
        fresh write is destroyed and only a self-healing miss hides the loss.
        """
        cache = cc.ContentCache(tmp_path, max_entries=1, max_total_bytes=None, default_ttl=60)
        key = cache.make_key("hot")
        cache.set(key, {"v": 1})
        # Evict the hot key through the LRU cap; its unlink is now pending.
        cache.set(cache.make_key("other"), {"v": 2})
        # The key is written again to the same deterministic path before the
        # pending unlink runs: the fresh write must survive.
        cache.set(key, {"v": "fresh"})
        assert cache.get(key) == {"v": "fresh"}
