"""LOW fix: robots_cache must be hard-capped so it cannot grow unbounded
within a single large crawl, even when every entry is still fresh.

Before the fix, _fetch_robots_rules only purged *expired* entries once the
cache exceeded 1000; a crawl discovering thousands of unique (fresh) domains
kept growing the cache. A module-level _cap_robots_cache evicts the oldest
entries by insertion order once over the limit.
"""
from app.crawl.engine import _cap_robots_cache


def test_cap_robots_cache_evicts_oldest_when_over_limit():
    cache = {}
    for i in range(5):
        # (expires_at, rules) tuples, inserted in order
        cache[f"origin{i}"] = (1000.0 + i, [("disallow", "/")])
    _cap_robots_cache(cache, 3)
    assert len(cache) == 3
    # Oldest two (origin0, origin1) evicted; newest three remain.
    assert set(cache.keys()) == {"origin2", "origin3", "origin4"}


def test_cap_robots_cache_noop_under_limit():
    cache = {f"origin{i}": (1000.0 + i, []) for i in range(3)}
    _cap_robots_cache(cache, 5)
    assert len(cache) == 3
