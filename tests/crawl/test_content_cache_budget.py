"""H9: the disk content cache needs a byte budget, not just a count cap.

max_entries=10000 bounds entry count, but each envelope can hold a
multi-MB base64 body -- worst case ~100GB of disk from crafted crawling.
Entries over the per-entry cap must be refused, and LRU eviction must
enforce a total byte budget.
"""

import pytest

from app.crawl.content_cache import ContentCache


def _make_cache(tmp_path, **kwargs):
    kwargs.setdefault("default_ttl", 60)
    return ContentCache(str(tmp_path), **kwargs)


def _fail_writes(cache, monkeypatch, *, only_when=None):
    """Patch ``_write_data`` to raise OSError.

    With ``only_when`` set, writes whose serialized data lacks that substring
    still hit the real writer, so a prior entry persists and only the targeted
    write fails. Shared by the write-failure/rollback tests (one seam, not a
    ``boom`` fake cloned per test).
    """
    real_write = cache._write_data

    def boom(path, data):
        if only_when is None or only_when in data:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(cache, "_write_data", boom)


class TestContentCacheByteBudget:
    def test_oversized_entry_is_refused(self, tmp_path):
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        cache.set("big", {"body": "x" * 10_000}, ttl=60)
        assert cache.get("big") is None
        assert list(tmp_path.glob("*.json")) == []

    def test_total_budget_evicts_lru_first(self, tmp_path):
        cache = _make_cache(
            tmp_path,
            max_entries=10_000,
            max_entry_bytes=10_000,
            max_total_bytes=400,
        )
        cache.set("k1", {"body": "a" * 100}, ttl=60)
        cache.set("k2", {"body": "b" * 100}, ttl=60)
        assert cache.get("k1") is not None
        assert cache.get("k2") is not None
        # Third write overflows the budget: k1 (least recently used) goes.
        cache.set("k3", {"body": "c" * 100}, ttl=60)
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None
        on_disk = sum(p.stat().st_size for p in tmp_path.glob("*.json"))
        assert on_disk <= 400

    async def test_aset_enforces_budget(self, tmp_path):
        cache = _make_cache(
            tmp_path,
            max_entries=10_000,
            max_entry_bytes=10_000,
            max_total_bytes=400,
        )
        await cache.aset("k1", {"body": "a" * 100}, ttl=60)
        await cache.aset("k2", {"body": "b" * 100}, ttl=60)
        await cache.aset("k3", {"body": "c" * 100}, ttl=60)
        assert await cache.aget("k1") is None
        assert await cache.aget("k3") is not None

    async def test_aset_oversized_never_written(self, tmp_path, monkeypatch):
        """N1: the async path must refuse oversized entries before any disk
        write -- the payload must not transiently hit disk (crash window).
        Mirrors set(): pre-serialize, size-check under the lock, then write.
        The spy sits on ``_write_data``, the seam the real code uses."""
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        calls: list[str] = []

        real_write = cache._write_data

        def spy_write(path, data):
            calls.append(str(path))
            real_write(path, data)

        monkeypatch.setattr(cache, "_write_data", spy_write)
        await cache.aset("big", {"body": "x" * 10_000}, ttl=60)
        assert calls == []
        assert list(tmp_path.glob("*.json")) == []
        assert await cache.aget("big") is None
        # A small write in the same cache still works.
        await cache.aset("ok", {"body": "y"}, ttl=60)
        assert await cache.aget("ok") is not None

    async def test_aset_accounting_matches_written_bytes(self, tmp_path):
        """N1: aset() byte accounting must equal the exact bytes on disk
        (cross-instance equality is nondeterministic: the envelope embeds
        clock() floats whose repr width varies between writes)."""
        cache_b = _make_cache(tmp_path / "b", max_entry_bytes=10_000)
        await cache_b.aset("same", {"body": "z" * 500}, ttl=60)
        path = cache_b._index["same"]
        assert cache_b._index_sizes["same"] == path.stat().st_size
        assert cache_b._index_total_bytes == path.stat().st_size

    async def test_aset_write_failure_leaves_no_dangling_index(self, tmp_path, monkeypatch):
        """N1: a failed disk write must roll the index entry back so aget()
        does not keep consulting a path that does not exist."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)

        def boom(path, envelope):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "_write_data", boom)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index
        assert cache._index_sizes.get("k", 0) == 0
        assert await cache.aget("k") is None

    def test_prune_repairs_unknown_sizes(self, tmp_path):
        """N1: prune() must reconcile byte accounting for index entries whose
        recorded size is missing (e.g. crash between index insert and
        accounting update) by stat'ing the surviving file."""
        import json as _json

        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("k", {"body": "v" * 300}, ttl=60)
        # Simulate crash residue: index entry survives, its recorded size
        # and the total were lost.
        cache._index_sizes.clear()
        cache._index_total_bytes = 0
        removed = cache.prune()
        assert removed == 0
        path = cache._index["k"]
        assert cache._index_sizes["k"] == path.stat().st_size
        assert cache._index_total_bytes == path.stat().st_size
        assert _json.loads(path.read_text())["value"] == {"body": "v" * 300}

    def test_set_routes_through_write_seam(self, tmp_path, monkeypatch):
        """#1733 twins: the sync write must go through _write_data like aset, so
        a failure is observable/testable at the same seam (a spy on an inline
        atomic_write_text call site is silently vacuous)."""
        cache = _make_cache(tmp_path)

        def boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "_write_data", boom)
        with pytest.raises(OSError):
            cache.set("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index

    async def test_aget_cold_reads_honor_mirror_count_cap(self, tmp_path):
        """#1733 twins: aget's disk-hit mirror enforces the same
        _MEMORY_CACHE_MAX cap as set/aset — otherwise repeated cold reads of
        distinct small keys grow the RAM mirror without bound."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000, max_total_bytes=10_000_000)
        keys = [f"k{i}" for i in range(cache._MEMORY_CACHE_MAX + 50)]
        for key in keys:
            await cache.aset(key, {"body": key}, ttl=60)
        cache._memory_cache.clear()  # simulate a cold process with a warm disk
        for key in keys:
            assert await cache.aget(key) == {"body": key}
        assert len(cache._memory_cache) <= cache._MEMORY_CACHE_MAX
        for key in keys[-cache._MEMORY_CACHE_MAX :]:
            assert key in cache._memory_cache  # the cap must evict OLDEST, not block new

    def test_get_disk_hit_mirrors_small_value(self, tmp_path):
        """#1733 twins: a hot URL read through the SYNC path warms the RAM
        mirror exactly like the async path (the accelerator contract is
        method-independent)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("hot", {"body": "small"}, ttl=60)
        cache._memory_cache.clear()
        assert cache.get("hot") == {"body": "small"}
        assert "hot" in cache._memory_cache

    async def test_aset_write_failure_rolls_back_stale_mirror(self, tmp_path, monkeypatch):
        """#1733 twins: when the async disk write fails with a surviving older
        entry, the in-memory mirror must not keep serving the value that was
        never persisted — rollback covers RAM, not just the index."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("k", {"body": "old"}, ttl=60)
        _fail_writes(cache, monkeypatch, only_when="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new"}, ttl=60)
        assert await cache.aget("k") == {"body": "old"}
        assert cache.get("k") == {"body": "old"}

    async def test_aset_new_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """L1: a failed write of a NEW key must not move _index_total_bytes.

        Bytes enter the total only at commit (after a successful write), so the
        old rollback's `total -= size` decremented bytes that were never added;
        the max(0, ...) clamp only hid it when the cache was otherwise empty."
        """
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("keep", {"body": "v" * 200}, ttl=60)  # counts once, stays
        total_before = cache._index_total_bytes
        assert total_before == sum(cache._index_sizes.values()) > 0

        _fail_writes(cache, monkeypatch)
        with pytest.raises(OSError):
            await cache.aset("ghost", {"body": "g" * 300}, ttl=60)
        assert "ghost" not in cache._index
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())

    async def test_aset_existing_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """L1: an existing key whose re-write fails keeps its prior bytes counted
        (the old rollback applied a spurious `prior_size - size` delta)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("k", {"body": "old"}, ttl=60)
        total_before = cache._index_total_bytes
        assert cache._index_sizes["k"] > 0
        _fail_writes(cache, monkeypatch, only_when="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new" * 100}, ttl=60)
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())
        assert await cache.aget("k") == {"body": "old"}


class TestExpiredEntryFreesByteAccounting:
    """reading an expired (or unreadable) entry popped ``_index`` but left
    ``_index_sizes``/``_index_total_bytes`` untouched. The phantom bytes keep
    ``_over_limits()`` true, so the next ``set()``'s ``_enforce_limits`` evicts
    LIVE LRU entries it should not -- a sustained self-inflicted cache wipe-out
    on a TTL-busy cache.
    """

    def test_expired_read_frees_byte_accounting(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            # Tight budget: two entries fit, three do not.
            max_total_bytes=10_000,
        )

        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        size_a = cache._index_sizes["a"]
        total_before = cache._index_total_bytes
        assert total_before > 0

        # Advance past the TTL and read the now-expired entry.
        now[0] = 2000.0
        assert cache.get("a") is None, "expired entry must read as None"

        # The expired read must free A's byte accounting (via _drop_locked),
        # not just the index entry.
        assert cache._index_total_bytes == total_before - size_a, (
            f"phantom bytes: total {cache._index_total_bytes} != "
            f"{total_before - size_a} (expired entry's {size_a} bytes still counted)"
        )

    def test_set_after_expired_read_does_not_evict_live_entry(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            max_total_bytes=10_000,
        )
        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        # Budget for exactly two entries.
        unit = cache._index_sizes["b"]
        cache.max_total_bytes = 2 * unit + unit // 2

        now[0] = 2000.0
        assert cache.get("a") is None  # expired read frees A's bytes (or not)
        now[0] = 1000.0  # keep B and C unexpired for the assertions

        cache.set("c", "x" * 100)
        # B is live and must not be sacrificed to phantom A bytes. Clear the
        # RAM mirror so the assertion reads the disk-backed index, not RAM.
        cache._memory_cache.clear()
        assert cache.get("b") == "x" * 100, "live LRU entry evicted by phantom bytes"
