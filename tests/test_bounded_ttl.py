"""BoundedTTLCache contract: lock-inside, TTL, LRU eviction (chokepoint #1)."""

import threading

from app.bounded_ttl import BoundedTTLCache


def _lru_with_a_b():
    """BoundedTTLCache (max_entries=2) pre-loaded with a and b."""
    clock = {"now": 1000.0}
    cache = BoundedTTLCache(max_entries=2, ttl=100.0, clock=lambda: clock["now"])
    cache.put("a", 1)
    cache.put("b", 2)
    return cache


class TestContract:
    def test_get_put_roundtrip_and_ttl_expiry(self):
        clock = {"now": 1000.0}
        cache = BoundedTTLCache(max_entries=4, ttl=10.0, clock=lambda: clock["now"])
        cache.put("a", 1)
        assert cache.get("a") == 1
        clock["now"] += 10.0  # exactly at expiry boundary: engines treat
        # ``expires_at > now`` as fresh, so the boundary itself is expired
        assert cache.get("a") is None
        clock["now"] -= 0.01
        # The engines' dicts keep expired entries present (purge-only
        # deletion, relied on by the sitemap 304 path); rewinding the clock
        # must resurrect the still-stored entry.
        assert cache.get("a") == 1
        clock["now"] += 10.01
        assert cache.purge_expired() == 1
        assert cache.get("a") is None

    def test_get_stale_returns_expired_entries_for_304_reuse(self):
        clock = {"now": 1000.0}
        cache = BoundedTTLCache(max_entries=4, ttl=10.0, clock=lambda: clock["now"])
        cache.put("u", (["/p1"], "v1", None))
        clock["now"] += 60.0  # expired
        assert cache.get("u") is None  # freshness still enforced on get
        assert cache.get_stale("u") == (["/p1"], "v1", None)  # present for 304 reuse
        assert cache.peek("missing") is None
        assert cache.get_stale("missing") is None

    def test_lru_eviction_beyond_max_entries(self):
        cache = _lru_with_a_b()
        assert cache.get("a") == 1  # refresh a's recency
        cache.put("c", 3)  # evicts b (LRU), not a
        assert cache.get("b") is None
        assert cache.get("a") == 1
        assert cache.get("c") == 3

    def test_purge_expired_returns_count(self):
        clock = {"now": 1000.0}
        cache = BoundedTTLCache(max_entries=8, ttl=10.0, clock=lambda: clock["now"])
        cache.put("a", 1)
        cache.put("b", 2, ttl=100.0)
        clock["now"] += 20.0
        assert cache.purge_expired() == 1
        assert len(cache) == 1
        assert cache.get("b") == 2

    def test_peek_does_not_refresh_recency(self):
        cache = _lru_with_a_b()
        assert cache.peek("a") == 1  # no recency refresh
        cache.put("c", 3)  # evicts a (still oldest)
        assert cache.get("a") is None

    def test_concurrent_put_get_are_consistent(self):
        cache = BoundedTTLCache(max_entries=256, ttl=100.0)
        errors: list[str] = []

        def worker(i: int) -> None:
            try:
                for round_ in range(200):
                    key = f"k{i % 16}-{round_ % 8}"
                    cache.put(key, i)
                    cache.get(key)
            except Exception as exc:  # pragma: no cover
                errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(cache) <= 256
