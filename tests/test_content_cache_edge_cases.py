"""Content cache edge cases: eviction, corruption, budget accounting."""

from coverage_helpers import _temp_cache


class TestContentCacheEdgeCases:
    """Cover remaining branches in content_cache.py"""

    def test_content_cache_get_invalid_json(self):
        import pathlib
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache_path = pathlib.Path(tmpdir) / "content.testkey.json"
            cache_path.write_text("not valid json")
            result = cache.get("testkey")
            assert (result) is None

    def test_content_cache_get_expired(self):
        cache, clock_vals = _temp_cache(ttl=1)
        cache.set("key", {"data": "value"})
        clock_vals[0] = 1002.0
        assert (cache.get("key")) is None

    def test_content_cache_prune(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = ContentCache(tmpdir, default_ttl=1, clock=lambda: clock_vals[0])
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            clock_vals[0] = 1002.0
            removed = cache.prune()
            assert removed == 2
            assert (cache.get("key1")) is None
            assert (cache.get("key2")) is None

    def test_content_cache_clear(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            removed = cache.clear()
            assert removed == 2
            assert (cache.get("key1")) is None
            assert (cache.get("key2")) is None

    async def test_aset_aget_roundtrip(self):
        """Adversarial perf #1.2/#2.2: async disk cache stores and retrieves,
        including a large (HTML-sized) value that hits the on-disk path."""
        import pathlib
        import tempfile

        from app.crawl.content_cache import ContentCache

        d = tempfile.mkdtemp()
        cache = ContentCache(cache_dir=pathlib.Path(d), default_ttl=60)
        big = "<html>" + ("x" * 2_000_000) + "</html>"
        await cache.aset("k1", big)
        # RAM mirror only keeps <1MiB, so this exercises the disk read path.
        got = await cache.aget("k1")
        assert got == big

    async def test_aset_writes_off_event_loop(self):
        """The disk write (json.dumps of a large value) runs in a worker thread."""
        import pathlib
        import tempfile
        import threading

        from app.crawl.content_cache import ContentCache

        d = tempfile.mkdtemp()
        cache = ContentCache(cache_dir=pathlib.Path(d), default_ttl=60)
        seen = {}

        def spy_write(path, data):
            seen["thread"] = threading.current_thread()
            path.write_text(data)

        cache._write_data = spy_write
        big = "y" * 2_000_000
        await cache.aset("k2", big)
        assert seen, "write never happened"
        assert seen["thread"] is not threading.current_thread()


class TestCachedAtReadsMetadataWithoutDeserializingTheValue:
    """P6: cached_at_of must not deserialize the whole envelope on every call.

    Sitemap freshness checks call this for many keys per crawl, and each
    envelope can hold a multi-MB body. Reading the value bytes off disk (and
    JSON-parsing them) to answer a timestamp question makes freshness O(bytes)
    instead of O(keys). The timestamp is now answered from a metadata mirror
    that is warmed on set and on the first read, with _read_envelope remaining
    the single chokepoint for full reads and for every state mutation.
    """

    def test_cached_at_after_set_reads_no_envelope(self):
        cache, _ = _temp_cache(ttl=60)
        cache.set("k", {"big": "x" * 100})
        cache.set("k", {"big": "y" * 100})  # ensure the mirror is refreshed

        calls = []
        original = cache._read_envelope

        def counting(key):
            calls.append(key)
            return original(key)

        cache._read_envelope = counting
        try:
            assert cache.cached_at_of("k") is not None
            assert cache.cached_at_of("k") is not None
        finally:
            cache._read_envelope = original
        assert calls == [], f"freshness check deserialized the envelope: {calls}"

    def test_cached_at_value_matches_the_envelope(self):
        cache, clock_vals = _temp_cache(ttl=60)
        cache.set("k", {"data": "value"})
        expected = clock_vals[0]
        assert cache.cached_at_of("k") == expected

    def test_cached_at_of_a_cold_on_disk_entry_answers_and_then_warms(self):
        """A warm restart (fresh instance, same directory) still answers
        correctly from disk, and the read pays for itself on later calls."""
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            clock = [1000.0]
            cache = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0])
            cache.set("k", {"data": "value"})
            cached_at = cache.cached_at_of("k")

            reopened = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0])
            calls = []
            original = reopened._read_envelope

            def counting(key):
                calls.append(key)
                return original(key)

            reopened._read_envelope = counting
            try:
                # Cold mirror: one full read is unavoidable and correct.
                assert reopened.cached_at_of("k") == cached_at
                # Now warm: later freshness checks never touch the envelope.
                assert reopened.cached_at_of("k") == cached_at
            finally:
                reopened._read_envelope = original
        assert len(calls) == 1, f"expected one cold read, got {calls}"

    def test_expired_entry_reports_none_through_the_chokepoint(self):
        """An expired mirror entry must still be expired and dropped by the
        shared chokepoint, not silently answered from stale metadata."""
        cache, clock_vals = _temp_cache(ttl=10)
        cache.set("k", {"data": "value"})
        assert cache.cached_at_of("k") is not None
        clock_vals[0] += 1000.0
        assert cache.cached_at_of("k") is None
        # The expired entry is gone from the index, not just unanswered.
        assert "k" not in cache._index

    def test_evicted_entry_is_not_answered_from_stale_metadata(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0], max_entries=1)
            cache.set("k1", {"data": "one"})
            assert cache.cached_at_of("k1") is not None
            # Evict k1 via the LRU cap.
            cache.set("k2", {"data": "two"})
            assert "k1" not in cache._index
            assert cache.cached_at_of("k1") is None
            assert cache.cached_at_of("k2") is not None
