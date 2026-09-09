"""TestContentCacheCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import _temp_cache


class TestContentCacheCoverageGaps:
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
