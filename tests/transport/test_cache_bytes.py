"""Byte budgets hold: caps, eviction and expiry free accounting."""

from __future__ import annotations

import json

import pytest

import crawlutron.transport.content_cache as cc
import crawlutron.transport.content_cache_internals as cci
from crawlutron.transport.content_cache import ContentCache
from tests._cache_fixtures import (
    _make_cache,
    _seeded_then_write_fails,
    counting_entry_reads,
    entry_path,
    on_disk_bytes,
    watch_writes,
)
from tests._fetcher_factories import make_expiring_cache


def budgeted_cache(tmp_path, *, max_total_bytes: int = 400, **settings) -> ContentCache:
    """A cache whose total byte budget is the only limit in play.

    The entry cap and the count cap are lifted out of the way, so a test about
    byte accounting cannot pass because the count cap evicted first.
    """
    return _make_cache(
        tmp_path,
        max_entries=10_000,
        max_entry_bytes=10_000,
        max_total_bytes=max_total_bytes,
        **settings,
    )


def _at_cap_budget() -> tuple[float, dict, int]:
    """``(now, value, cap)`` where ``cap`` is exactly the on-disk envelope size.

    The budget is the envelope's bytes, not the value's, so the two are computed
    together here -- a change to either moves both.
    """
    now = 1000.0
    value = {"body": "x" * 150}
    envelope = {"expires": now + 60, "value": value, "cached_at": now}
    cap = len(json.dumps(envelope))
    assert cap > len(json.dumps(value))
    return now, value, cap


class TestContentCacheByteBudget:
    def test_oversized_entry_is_refused(self, tmp_path):
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        cache.set("big", {"body": "x" * 10_000}, ttl=60)
        assert cache.get("big") is None
        assert list(cache.cache_dir.iterdir()) == []

    def test_entry_at_cap_is_accepted_with_envelope_accounting(self, tmp_path):
        now, value, cap = _at_cap_budget()
        cache = _make_cache(tmp_path, max_entry_bytes=cap, clock=lambda: now)
        cache.set("at-cap", value, ttl=60)

        assert cache.get("at-cap") == value
        assert entry_path(cache, "at-cap").stat().st_size == cap

    async def test_async_entry_at_cap_is_accepted_with_envelope_accounting(self, tmp_path):
        now, value, cap = _at_cap_budget()
        cache = _make_cache(tmp_path / "async", max_entry_bytes=cap, clock=lambda: now)

        await cache.aset("at-cap", value, ttl=60)

        assert await cache.aget("at-cap") == value
        assert entry_path(cache, "at-cap").stat().st_size == cap

    def test_total_budget_evicts_lru_first(self, tmp_path):
        cache = budgeted_cache(tmp_path)
        cache.set("k1", {"body": "a" * 100}, ttl=60)
        cache.set("k2", {"body": "b" * 100}, ttl=60)
        assert cache.get("k1") is not None
        assert cache.get("k2") is not None
        # Third write overflows the budget: k1 (least recently used) goes.
        cache.set("k3", {"body": "c" * 100}, ttl=60)
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None
        on_disk = sum(p.stat().st_size for p in cache.cache_dir.iterdir())
        assert on_disk <= 400

    async def test_aset_enforces_budget(self, tmp_path):
        cache = budgeted_cache(tmp_path)
        await cache.aset("k1", {"body": "a" * 100}, ttl=60)
        await cache.aset("k2", {"body": "b" * 100}, ttl=60)
        await cache.aset("k3", {"body": "c" * 100}, ttl=60)
        assert await cache.aget("k1") is None
        assert await cache.aget("k3") is not None

    async def test_aset_oversized_never_written(self, tmp_path, monkeypatch):
        """The async path must refuse oversized entries before any disk write.

        Mirrors set(): pre-serialize, size-check under the lock, then write, so the
        spy sits on ``_write_data``, the seam the real code uses.
        """
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        written: list[str] = []

        watch_writes(cache, calls=written)
        await cache.aset("big", {"body": "x" * 10_000}, ttl=60)
        assert written == []
        assert list(cache.cache_dir.iterdir()) == []
        assert await cache.aget("big") is None
        # A small write in the same cache still works.
        await cache.aset("ok", {"body": "y"}, ttl=60)
        assert await cache.aget("ok") is not None

    async def test_aset_accounting_matches_written_bytes(self, tmp_path):
        """Aset() byte accounting must equal the exact bytes on disk
        (cross-instance equality is nondeterministic: the envelope embeds
        clock() floats whose repr width varies between writes)."""
        cache_b = _make_cache(tmp_path / "b", max_entry_bytes=10_000)
        await cache_b.aset("same", {"body": "z" * 500}, ttl=60)
        # The accounted on-disk footprint is exactly the one written envelope.
        assert on_disk_bytes(cache_b) == entry_path(cache_b, "same").stat().st_size

    async def test_aset_write_failure_leaves_no_dangling_index(self, tmp_path, monkeypatch):
        """A failed disk write must roll the index entry back so aget()
        does not keep consulting a path that does not exist."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "v"}, ttl=60)
        assert on_disk_bytes(cache) == 0
        assert await cache.aget("k") is None

    def test_total_bytes_accounts_live_entries(self, tmp_path):
        """The on-disk footprint tracks the live envelopes: it grows when an
        entry is added and clears when the cache is emptied, so the eviction
        budget never sees phantom bytes."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("k1", {"body": "v" * 100}, ttl=60)
        cache.set("k2", {"body": "w" * 200}, ttl=10)
        assert (
            on_disk_bytes(cache)
            == entry_path(cache, "k1").stat().st_size + entry_path(cache, "k2").stat().st_size
        )
        assert cache.clear() == 2
        assert on_disk_bytes(cache) == 0

    def test_set_routes_through_write_seam(self, tmp_path, monkeypatch):
        """Twins: the sync write must go through _write_data like aset, so
        a failure is observable/testable at the same seam (a spy on an inline
        atomic_write_text call site is silently vacuous)."""
        cache = _make_cache(tmp_path)
        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            cache.set("k", {"body": "v"}, ttl=60)
        assert on_disk_bytes(cache) == 0

    def test_cache_writes_are_best_effort(self, tmp_path, monkeypatch):
        """The disk writer declares durable=False: entries are reproducible, so a
        power loss may drop one whole but the fsync-per-store ceiling is gone (#357)."""
        kwargs: list[dict] = []
        real_write = cci.atomic_write_text

        def spy(path, data, **kw):
            kwargs.append(kw)
            return real_write(path, data, **kw)

        monkeypatch.setattr(cci, "atomic_write_text", spy)
        cache = _make_cache(tmp_path)
        cache.set("k", {"body": "v"}, ttl=60)
        assert on_disk_bytes(cache) > 0
        assert kwargs == [{"durable": False}], kwargs

    async def test_aget_cold_reads_honor_mirror_count_cap(self, tmp_path, monkeypatch):
        """Twins: aget's disk-hit mirror enforces the same count cap as
        set/aset — otherwise repeated cold reads of distinct small keys grow the
        RAM mirror without bound."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000, max_total_bytes=10_000_000)
        mirror_cap = cc.MEMORY_CACHE_MAX
        keys = [f"k{i}" for i in range(mirror_cap + 50)]
        for key in keys:
            await cache.aset(key, {"body": key}, ttl=60)
        for key in keys:
            assert await cache.aget(key) == {"body": key}
        # The mirror dropped the OLDEST key (evicted past the cap) so it now
        # needs a disk read, while the newest is still served from RAM.
        with counting_entry_reads(monkeypatch, cache, keys[-mirror_cap:][0]) as reads:
            assert await cache.aget(keys[-mirror_cap:][0]) == {"body": keys[-mirror_cap:][0]}
            assert reads == [0]
        with counting_entry_reads(monkeypatch, cache, keys[0]) as reads:
            assert await cache.aget(keys[0]) == {"body": keys[0]}
            assert reads == [1]

    def test_oversized_values_are_never_ram_mirrored(self, tmp_path, monkeypatch):
        """The small-value RAM mirror must not hold oversized envelopes: a
        value past the mirror size gate is served from disk on every read,
        keeping RAM bounded for hot reads."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000_000)
        cache.set("big", {"body": "x" * (1024 * 1024)}, ttl=60)
        # Mirror gate holds even after a hot read serves the value from disk.
        with counting_entry_reads(monkeypatch, cache, "big") as reads:
            assert cache.get("big") == {"body": "x" * (1024 * 1024)}
            assert reads == [1]
        cache.set("small", {"body": "y"}, ttl=60)
        with counting_entry_reads(monkeypatch, cache, "small") as reads:
            assert cache.get("small") == {"body": "y"}
            assert reads == [0]

    async def test_aset_write_failure_rolls_back_stale_mirror(self, tmp_path, monkeypatch):
        """Twins: when the async disk write fails with a surviving older
        entry, the in-memory mirror must not keep serving the value that was
        never persisted — rollback covers RAM, not just the index."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await _seeded_then_write_fails(cache, monkeypatch)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new"}, ttl=60)
        assert await cache.aget("k") == {"body": "old"}
        assert cache.get("k") == {"body": "old"}

    async def test_aset_new_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """A failed write of a NEW key must not move the byte total: bytes
        enter the accounting only at commit, never before a successful write."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("keep", {"body": "v" * 200}, ttl=60)  # counts once, stays
        total_before = on_disk_bytes(cache)
        assert total_before > 0

        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            await cache.aset("ghost", {"body": "g" * 300}, ttl=60)
        assert on_disk_bytes(cache) == total_before
        assert await cache.aget("ghost") is None

    async def test_aset_existing_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """An existing key whose re-write fails keeps its prior bytes counted
        (the old rollback applied a spurious `prior_size - size` delta)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await _seeded_then_write_fails(cache, monkeypatch)
        total_before = on_disk_bytes(cache)
        assert total_before > 0
        watch_writes(cache, fail="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new" * 100}, ttl=60)
        assert on_disk_bytes(cache) == total_before
        assert await cache.aget("k") == {"body": "old"}


class TestExpiredEntryFreesByteAccounting:
    """Reading an expired entry popped the index but left byte accounting untouched.

    The phantom bytes keep the eviction budget over, so the next set evicts LIVE
    LRU entries it should not -- a self-inflicted wipe on a TTL-busy cache.
    """

    def test_set_after_expired_read_does_not_evict_live_entry(self, tmp_path):
        """An expired read must free the entry's bytes: phantom bytes would
        keep the eviction budget over and the next set would evict a LIVE LRU
        entry it should not -- a self-inflicted wipe on a TTL-busy cache."""
        cache, now = make_expiring_cache(tmp_path / "cache")
        # Budget for exactly two entries.
        unit = entry_path(cache, "b").stat().st_size
        cache.max_total_bytes = 2 * unit + unit // 2

        now[0] = 2000.0
        assert cache.get("a") is None  # expired read frees A's bytes (or not)
        now[0] = 1000.0  # keep B and C unexpired for the assertions

        cache.set("c", "x" * 100)
        assert cache.get("b") == "x" * 100, "live LRU entry evicted by phantom bytes"
