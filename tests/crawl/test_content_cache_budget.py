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
        Mirrors set(): pre-serialize, size-check under the lock, then write."""
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        calls: list[str] = []

        real_write = cache._write_envelope

        def spy_write(path, envelope):
            calls.append(str(path))
            real_write(path, envelope)

        monkeypatch.setattr(cache, "_write_envelope", spy_write)
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
