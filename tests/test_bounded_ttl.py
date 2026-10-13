"""BoundedTTLCache contract: lock-inside, TTL, LRU eviction."""

from typing import Any

from app.safety.bounded_ttl import BoundedTTLCache
from tests._thread_fanout import run_thread_fanout


def _clocked_cache(ttl: float = 10.0) -> tuple[dict[str, float], BoundedTTLCache[str, Any]]:
    """A cache whose clock the test owns, and the handle to move it.

    These tests are about what expiry *means* at the boundary, so none of them
    can use a real clock -- and each was building the same clock-plus-cache pair
    before making a different assertion about it. The returned dict is the verb:
    moving ``now`` is the only thing the caller does.
    """
    clock: dict[str, float] = {"now": 1000.0}
    cache = BoundedTTLCache[str, Any](max_entries=4, ttl=ttl, clock=lambda: clock["now"])
    return clock, cache


def _lru_with_a_b():
    """BoundedTTLCache (max_entries=2) pre-loaded with a and b."""
    clock: dict[str, float] = {"now": 1000.0}
    cache = BoundedTTLCache[str, Any](max_entries=2, ttl=100.0, clock=lambda: clock["now"])
    cache.put("a", 1)
    cache.put("b", 2)
    return cache


class TestContract:
    def test_get_put_roundtrip_and_ttl_expiry(self):
        clock, cache = _clocked_cache()
        cache.put("a", 1)
        assert cache.get("a") == 1
        clock["now"] += 10.0  # exactly at expiry boundary: engines treat
        # ``expires_at > now`` as fresh, so the boundary itself is expired
        assert cache.get("a") is None
        clock["now"] -= 0.01
        # The engines' dicts keep expired entries present (purge-only deletion, relied on by the sitemap 304
        # path); rewinding the clock must resurrect the still-stored entry.
        assert cache.get("a") == 1
        clock["now"] += 10.01
        assert cache.purge_expired() == 1
        assert cache.get("a") is None

    def test_get_stale_returns_expired_entries_for_304_reuse(self):
        clock, cache = _clocked_cache()
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
        clock: dict[str, float] = {"now": 1000.0}
        cache = BoundedTTLCache[str, Any](max_entries=8, ttl=10.0, clock=lambda: clock["now"])
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
        cache = BoundedTTLCache[str, Any](max_entries=256, ttl=100.0)

        def worker(i: int) -> None:
            for round_ in range(200):
                key = f"k{i % 16}-{round_ % 8}"
                cache.put(key, i)
                cache.get(key)

        errors = run_thread_fanout(worker)
        assert not errors, errors
        assert len(cache) <= 256
