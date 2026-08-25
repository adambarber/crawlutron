"""#7: ContentCache keeps an in-memory LRU of small values to skip disk reads."""

import shutil

import app.crawl.content_cache as cc


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
