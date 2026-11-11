"""One shared freshness engine drives both caches' TTL/expiry decisions (#124)."""

import crawlutron.transport.freshness as freshness
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from tests._clocks import Timeline
from tests._policy_factories import canonical


def _spy_on_engine(monkeypatch, mod) -> list[str]:
    """Route mod's freshness calls through a recorder, keeping the real engine."""
    calls: list[str] = []
    real_expiry = mod.expiry_for
    real_expired = mod.is_expired

    def spy_expiry(url, ttl):
        calls.append("expiry")
        return real_expiry(url, ttl)

    def spy_expired(expires_at, now):
        calls.append("expired")
        return real_expired(expires_at, now)

    monkeypatch.setattr(mod, "expiry_for", spy_expiry)
    monkeypatch.setattr(mod, "is_expired", spy_expired)
    return calls


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
    import crawlutron.transport.cache as cache_mod

    assert cache_mod.is_expired is freshness.is_expired
    assert cache_mod.expiry_for is freshness.expiry_for
    calls = _spy_on_engine(monkeypatch, cache_mod)
    cache = HttpCache(ttl_seconds=10, clock=Timeline())
    cache.store(canonical("http://x/1"), {"etag": '"v1"'})
    cache.get(canonical("http://x/1"))
    assert "expiry" in calls, "HttpCache.store did not route expiry through the engine"
    assert "expired" in calls, "HttpCache.get did not route freshness through the engine"


def test_content_cache_routes_ttl_decisions_through_the_engine(tmp_path, monkeypatch):
    import crawlutron.transport.content_cache as content_mod

    assert content_mod.is_expired is freshness.is_expired
    assert content_mod.expiry_for is freshness.expiry_for
    calls = _spy_on_engine(monkeypatch, content_mod)
    cache = ContentCache(tmp_path / "c", clock=Timeline())
    cache.set("k", {"etag": '"v1"'}, ttl=10)
    cache.get("k")
    assert "expiry" in calls, "ContentCache.set did not route expiry through the engine"
    assert "expired" in calls, "ContentCache.get did not route freshness through the engine"


def test_the_two_cache_classes_remain_distinct():
    assert HttpCache is not ContentCache
    assert issubclass(HttpCache, object)
    assert issubclass(ContentCache, object)
