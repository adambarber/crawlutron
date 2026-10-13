"""Memory tier, entry lifecycle and unlink discipline hold."""

import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

import app.transport.content_cache as cc
from tests._fetcher_factories import make_seeded_cache
from tests.crawl._cache_fixtures import (
    reopen_cache,
    scoped_cache,
    watch_writes,
    watching_entry_unlinks,
    watching_envelope_reads,
)


def _make(tmp_path, clock=1000.0):
    shutil.rmtree(tmp_path, ignore_errors=True)
    return cc.ContentCache(tmp_path, namespace="mem", clock=lambda: clock, max_entries=10000)


def test_small_value_lives_in_memory(tmp_path):
    c = _make(tmp_path)
    c.set("a", {"x": 1})
    assert c.get("a") == {"x": 1}
    assert "a" in c._memory_cache


def test_large_value_not_in_memory(tmp_path):
    c = _make(tmp_path)
    big = "z" * (2 * 1024 * 1024)  # 2MB string -> shallow size exceeds 1MB
    c.set("big", big)
    assert "big" not in c._memory_cache
    # Still resolvable from disk.
    assert c.get("big") == big


def test_memory_ttl_honored(tmp_path):
    c = _make(tmp_path)
    c.set("k", "v", ttl=60)  # expires at 1060
    assert c.get("k") == "v"
    c.clock = lambda: 2000.0
    assert c.get("k") is None
    # Expired entry evicted from memory cache.
    assert "k" not in c._memory_cache


def test_memory_lru_cap(tmp_path):
    c = _make(tmp_path)
    for i in range(150):
        c.set(f"k{i}", i)
    assert len(c._memory_cache) <= c._MEMORY_CACHE_MAX
    # Oldest evicted from memory, but still resolvable from disk.
    assert "k0" not in c._memory_cache
    assert "k149" in c._memory_cache
    assert c.get("k0") == 0


def test_clear_clears_memory(tmp_path):
    c = _make(tmp_path)
    c.set("a", 1)
    assert "a" in c._memory_cache
    c.clear()
    assert len(c._memory_cache) == 0


class TestContentCacheEntryLifecycle:
    """Corrupted entries, expiry, prune/clear and async disk writes"""

    def test_content_cache_get_expired(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1)
        clock[0] = 1002.0
        assert cache.get("key") is None

    @pytest.mark.parametrize("drop", ["prune", "clear"])
    def test_content_cache_drops_entries_and_reports_the_count(self, tmp_path, drop):
        """Whatever retired them, the entries are gone and the count said so.

        ``prune`` retires on TTL and ``clear`` retires unconditionally; the
        property under test is identical, so it is one test over the two
        operations. Advancing the clock for both costs nothing and keeps the
        fixture to one shape -- spelled per operation it was two copies of the
        same four assertions, which is how the ``clear`` copy ended up not
        checking that the count matched the entries that disappeared.
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
            seen: dict[str, Any] = {}
            watch_writes(cache, on_write=lambda path, data: seen.update(thread=threading.current_thread()))
            await cache.aset("k2", "y" * 2_000_000)
        assert seen, "write never happened"
        assert seen["thread"] is not threading.current_thread()


class TestCachedAtReadsMetadataWithoutDeserializingTheValue:
    """Cached_at_of must not deserialize the whole envelope on every call.

    Sitemap freshness checks call this for many keys per crawl, and each
    envelope can hold a multi-MB body. Reading the value bytes off disk (and
    JSON-parsing them) to answer a timestamp question makes freshness O(bytes)
    instead of O(keys). The timestamp is now answered from a metadata mirror
    that is warmed on set and on the first read, with _read_envelope remaining
    the single chokepoint for full reads and for every state mutation.
    """

    def test_cached_at_after_set_reads_no_envelope(self, tmp_path):
        cache, _ = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"big": "x" * 100})
        cache.set("k", {"big": "y" * 100})  # ensure the mirror is refreshed

        with watching_envelope_reads(cache) as reads:
            assert cache.cached_at_of("k") is not None
            assert cache.cached_at_of("k") is not None
        assert reads == [], f"freshness check deserialized the envelope: {reads}"

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
        with watching_envelope_reads(reopened) as reads:
            # Cold mirror: one full read is unavoidable and correct.
            assert reopened.cached_at_of("k") == cached_at
            # Now warm: later freshness checks never touch the envelope.
            assert reopened.cached_at_of("k") == cached_at
        assert len(reads) == 1, f"expected one cold read, got {reads}"

    def test_expired_entry_reports_none_through_the_chokepoint(self, tmp_path):
        """An expired mirror entry must still be expired and dropped by the
        shared chokepoint, not silently answered from stale metadata."""
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=10, value={"data": "value"})
        assert cache.cached_at_of("k") is not None
        clock[0] += 1000.0
        assert cache.cached_at_of("k") is None
        # The expired entry is gone from the index, not just unanswered.
        assert "k" not in cache._index

    def test_evicted_entry_is_not_answered_from_stale_metadata(self, tmp_path):
        with scoped_cache(default_ttl=60, clock=lambda: 1000.0, max_entries=1) as cache:
            cache.set("k1", {"data": "one"})
            assert cache.cached_at_of("k1") is not None
            # Evict k1 via the LRU cap.
            cache.set("k2", {"data": "two"})
            assert "k1" not in cache._index
            assert cache.cached_at_of("k1") is None
            assert cache.cached_at_of("k2") is not None


class TestContentCacheUnlinkDiscipline:
    def test_eviction_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """Eviction mutates the index/byte accounting under the lock and unlinks
        the doomed files only after releasing it: the event-loop thread
        contends on this lock, so disk I/O must never hold it."""
        cache = cc.ContentCache(tmp_path, max_entries=2, max_total_bytes=None, default_ttl=0)
        with watching_entry_unlinks(monkeypatch, cache) as held:
            # Over-cap by one: the oldest entry is evicted and its file unlinked.
            cache.set(cache.make_key("a"), {"v": 1})
            cache.set(cache.make_key("b"), {"v": 2})
            cache.set(cache.make_key("c"), {"v": 3})

        assert held, "the eviction unlinked nothing"
        assert not any(held), "unlink ran while holding the cache lock"
        assert cache.get(cache.make_key("a")) is None
        assert cache.get(cache.make_key("c")) == {"v": 3}

    def test_expiry_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """The TTL-expiry path in _read_envelope unlinks outside the lock too."""
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1, value={"data": "value"})
        with watching_entry_unlinks(monkeypatch, cache) as held:
            clock[0] = 1002.0
            assert cache.get("key") is None
        assert held, "the expiry unlinked nothing"
        assert not any(held), "expiry unlink ran while holding the cache lock"

    def test_re_set_key_survives_its_stale_eviction_unlink(self, tmp_path):
        """A key re-set after its eviction reuses the same deterministic path.

        The deferred unlink must spare a file that has become live again,
        otherwise the fresh write is destroyed and only a self-healing cache
        miss hides the loss.
        """
        cache = cc.ContentCache(tmp_path, max_entries=1, max_total_bytes=None, default_ttl=0)
        key = cache.make_key("hot")
        cache.set(key, {"v": 1})

        # Evict exactly as _enforce_limits does, but leave the unlink pending so the re-registration can land
        # inside the deferral window.
        doomed: list[tuple[str | None, Path]] = []
        with cache._lock:
            cache._drop_locked(key, doomed)

        # The key is written again to the same deterministic path.
        cache.set(key, {"v": "fresh"})
        cache._unlink_doomed(doomed)

        assert cache.get(key) == {"v": "fresh"}
