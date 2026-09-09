from app.crawl.content_cache import ContentCache


def test_default_max_entries_is_bounded_not_none():
    """ContentCache has a bounded default max_entries (F6)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600)
        # The default should be a positive integer, not None (which meant unbounded).
        assert c.max_entries is not None
        assert isinstance(c.max_entries, int)
        assert c.max_entries > 0


def test_max_entries_enforces_bound(tmp_path):
    """Cache never exceeds max_entries (F6)."""
    c = ContentCache(cache_dir=tmp_path / "bound", default_ttl=3600, max_entries=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    # 3 entries
    assert c.get("a") == 1
    assert c.get("b") == 2
    assert c.get("c") == 3

    # Add 'd' -> one must be evicted to stay at 3
    c.set("d", 4)
    # Total cached entries should be 3
    entries = list((tmp_path / "bound").glob("content.*.json"))
    assert len(entries) == 3
    # All remaining gets should work or return None (evicted)
    # At least one of a,b,c was evicted; d is present
    assert c.get("d") == 4
    # Verify we didn't lose all old entries (at least 2 of a,b,c remain)
    remaining = sum(1 for k in ("a", "b", "c") if c.get(k) is not None)
    assert remaining >= 2


def test_explicit_max_entries_zero_disables_limit():
    """max_entries=0 means no limit (opt-out)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600, max_entries=0)
        # Should not enforce
        for i in range(15000):
            c.set(f"k{i}", i)
        # All should be present (no eviction)
        assert c.get("k0") == 0
        assert c.get("k14999") == 14999


def test_lru_keeps_recently_used_entries():
    """Accessing an entry protects it from next eviction (#5)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600, max_entries=3)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # Touch "a" so it becomes most-recently-used.
        assert c.get("a") == 1
        # Push past the cap twice; "a" must survive both evictions.
        c.set("d", 4)
        c.set("e", 5)
        assert c.get("a") == 1
        # And the newest entries are present.
        assert c.get("e") == 5
