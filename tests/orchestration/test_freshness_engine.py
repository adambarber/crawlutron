"""One shared freshness engine drives both caches' TTL/expiry decisions (#124)."""

import app.transport.freshness as freshness
from app.transport.cache import HttpCache
from app.transport.content_cache import ContentCache


class _Clock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t


def test_expiry_from_ttl_is_one_rule():
    assert freshness.expiry_for(1000.0, 100) == 1100.0
    assert freshness.expiry_for(1000.0, 0) is None
    assert freshness.expiry_for(1000.0, -5) is None


def test_freshness_boundary_is_one_rule():
    assert freshness.is_expired(1100.0, 1100.0) is True
    assert freshness.is_expired(1099.0, 1100.0) is False
    assert freshness.is_expired(1000.0, None) is False


def test_http_cache_routes_ttl_decisions_through_the_engine(monkeypatch):
    # Both caches bound the SAME engine functions: one rule, written once.
    import app.transport.cache as cache_mod

    assert cache_mod.is_expired is freshness.is_expired
    assert cache_mod.expiry_for is freshness.expiry_for
    calls: list[str] = []
    real_expiry = cache_mod.expiry_for
    real_expired = cache_mod.is_expired
    monkeypatch.setattr(cache_mod, "expiry_for", lambda n, t: calls.append("expiry") or real_expiry(n, t))
    monkeypatch.setattr(cache_mod, "is_expired", lambda n, e: calls.append("expired") or real_expired(n, e))
    clock = _Clock()
    cache = HttpCache(ttl_seconds=10, clock=clock)
    cache.store("http://x/1", {"etag": '"v1"'})
    cache.get("http://x/1")
    assert "expiry" in calls, "HttpCache.store did not route expiry through the engine"
    assert "expired" in calls, "HttpCache.get did not route freshness through the engine"


def test_content_cache_routes_ttl_decisions_through_the_engine(tmp_path, monkeypatch):
    import app.transport.content_cache as content_mod

    assert content_mod.is_expired is freshness.is_expired
    assert content_mod.expiry_for is freshness.expiry_for
    calls: list[str] = []
    real_expiry = content_mod.expiry_for
    real_expired = content_mod.is_expired
    monkeypatch.setattr(content_mod, "expiry_for", lambda n, t: calls.append("expiry") or real_expiry(n, t))
    monkeypatch.setattr(content_mod, "is_expired", lambda n, e: calls.append("expired") or real_expired(n, e))
    clock = _Clock()
    cache = ContentCache(tmp_path / "c", clock=clock)
    cache.set("k", {"etag": '"v1"'}, ttl=10)
    cache.get("k")
    assert "expiry" in calls, "ContentCache.set did not route expiry through the engine"
    assert "expired" in calls, "ContentCache.get did not route freshness through the engine"


def test_the_two_cache_classes_remain_distinct():
    assert HttpCache is not ContentCache
    assert issubclass(HttpCache, object)
    assert issubclass(ContentCache, object)
