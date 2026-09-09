"""H9: the disk content cache needs a byte budget, not just a count cap.

max_entries=10000 bounds entry count, but each envelope can hold a
multi-MB base64 body -- worst case ~100GB of disk from crafted crawling.
Entries over the per-entry cap must be refused, and LRU eviction must
enforce a total byte budget.
"""

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
