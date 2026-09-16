"""robots.txt parsing, matching, caching and crawl-delay behavior."""

import threading

from app.crawl import engine as engine_mod


class TestRobotsCacheLocking:
    def test_robots_cache_lock_is_present(self):

        # The chokepoint moved the lock INSIDE the cache: the module no longer
        # carries a bare dict + lock pair that callers must compose correctly.
        cache = engine_mod.robots_cache
        assert hasattr(cache, "_lock")
        assert isinstance(cache._lock, type(threading.Lock()))
        assert hasattr(cache, "put") and hasattr(cache, "purge_expired")
