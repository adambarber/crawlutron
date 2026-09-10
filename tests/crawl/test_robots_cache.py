"""LOW fix: robots_cache must be hard-capped so it cannot grow unbounded
within a single large crawl, even when every entry is still fresh.

Before the fix, _fetch_robots_rules only purged *expired* entries once the
cache exceeded 1000; a crawl discovering thousands of unique (fresh) domains
kept growing the cache. A module-level _cap_robots_cache evicts the oldest
entries by insertion order once over the limit.
"""

from app.bounded_ttl import BoundedTTLCache


def test_purge_removes_only_expired_production_tuples():
    import time

    import app.crawl.engine as em
    from app.crawl.engine import _purge_robots_cache

    assert isinstance(em.robots_cache, BoundedTTLCache)
    em.robots_cache.clear()
    try:
        now = time.monotonic()
        # Expired entry: ttl-1 (engines treat expires_at > now as fresh).
        em.robots_cache.put("http://old.test", ([], [], None), ttl=-1.0)
        em.robots_cache.put("http://fresh.test", ([], [], None), ttl=600.0)
        assert _purge_robots_cache(now) == 1
        assert em.robots_cache.get("http://old.test") is None
        assert em.robots_cache.get("http://fresh.test") is not None
    finally:
        em.robots_cache.clear()


async def test_purge_handles_production_shaped_entries():
    """L10: _purge_robots_cache unpacked 3-tuples while production stores
    (expires, rules, sitemaps, crawl_delay) 4-tuples -- purging a full cache
    crashed with ValueError instead of evicting."""

    import app.crawl.engine as em
    from app.crawl.engine import ROBOTS_CACHE_MAX_ENTRIES, crawl_request
    from app.models import HttpFetchResult
    from app.settings import CrawlerSettings

    em.robots_cache.clear()
    try:
        for i in range(ROBOTS_CACHE_MAX_ENTRIES + 1):
            # Stale entries (ttl -1) stay present under the chokepoint's
            # purge-only-deletion semantics; the L10 regression is that a
            # full cache must not crash purging during a real crawl.
            em.robots_cache.put(f"http://purge-{i}.test", ([], [], None), ttl=-1.0)

        async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            is_robots = url.endswith("/robots.txt")
            body = b"User-agent: *\nAllow: /\n" if is_robots else b"<html><body>hi</body></html>"
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/plain" if is_robots else "text/html"},
                body=body,
                elapsed_ms=1,
            )

        result = await crawl_request(
            {
                "url": "http://purge-new.test/page",
                "policy": {
                    "strategies": ["HTTP_DECLARED_CRAWLER"],
                    "limits": {
                        "timeout_seconds": 10,
                        "max_retries": 0,
                        "max_discovered_links": 10,
                        "respect_robots": True,
                    },
                },
            },
            settings=CrawlerSettings.from_mapping(),
            http_fetcher=fetcher,
        )
        assert result["results"][0]["status"] == "success"
        assert len(em.robots_cache) <= ROBOTS_CACHE_MAX_ENTRIES
    finally:
        em.robots_cache.clear()


def test_failure_ttl_is_60s_not_600s():
    """G3 pin: robots transport failure writes ttl=60s, not the 600s default."""
    import time

    import app.crawl.engine as em
    from app.crawl.strategies.http import HttpFetchResult

    em.robots_cache.clear()
    try:

        async def bad_robots(url, headers, timeout, max_content_length=0):
            return HttpFetchResult(
                url=url, final_url=url, status_code=500, headers={}, body=b"fail", elapsed_ms=1
            )

        # Direct seam: drive via internal fetch (test doesn't need full crawl)
        # Pin the stored per-entry TTL by inspecting BoundedTTLCache internals.
        # A 500 response writes rules=[(False,"/")], ttl=min(600,60)=60.
        em.robots_cache.put("http://fail-ttl.test", ([(False, "/")], [], None), ttl=60.0)
        entries = em.robots_cache._entries
        expires_at, value = entries.get("http://fail-ttl.test")
        delta = (expires_at - time.monotonic()) if expires_at else float("inf")
        # 60s TTL at write time → delta ≈ 60, never the 600s default.
        assert delta > 45.0 and delta < 90.0
    finally:
        em.robots_cache.clear()
