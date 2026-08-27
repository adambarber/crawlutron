"""TestContentCacheCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestContentCacheCoverageGaps:
    """Cover remaining branches in content_cache.py"""

    def test_content_cache_get_invalid_json(self):
        from app.crawl.content_cache import ContentCache
        import tempfile
        import pathlib
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
        from app.crawl.content_cache import ContentCache
        import tempfile
        import time
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
        from app.crawl.content_cache import ContentCache
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            removed = cache.clear()
            assert removed == 2
            assert (cache.get("key1")) is None
            assert (cache.get("key2")) is None


