"""#675 item 2: a failed sitemap walk must not overwrite a good cache entry."""

from __future__ import annotations

from crawlutron.transport.fetch_helpers import SITEMAP_CACHE, fetch_sitemap_urls
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


def _failing_fetcher(status: int):
    return FetcherDouble(lambda url, *_: {"status": status, "ctype": "application/xml", "body": b""})


def _seed_expired_good_entry(url, *, urls=("http://x.test/p1",)):
    """An expired cached sitemap entry carrying a good URL set (stale-readable)."""
    SITEMAP_CACHE.put(
        (url.raw, UNRESTRICTED_HOST_POLICY),
        (list(urls), "good-etag", None),
        ttl=-1.0,
    )


def _loc_raws(urls) -> list[str]:
    """The raw spellings of a cached/stale URL set: entries may predate
    normalization (the suite seeds plain strings, as the 304 path stores)."""
    items = [u if isinstance(u, tuple) else (u, None) for u in urls]
    return [u.raw if hasattr(u, "raw") else str(u) for u, _lastmod in items]


async def test_red_failed_walk_keeps_the_last_good_cached_entry():
    """A stale-but-present good entry, then a failed walk (500): the entry must
    survive stale-readable (304-reusable), never displaced by an empty
    failure-TTL result that outlives the good set."""
    url = canonical("http://x.test/sitemap.xml")
    key = (url.raw, UNRESTRICTED_HOST_POLICY)
    SITEMAP_CACHE.discard(key)
    try:
        _seed_expired_good_entry(url)

        out = await fetch_sitemap_urls(_failing_fetcher(500), url, 10, host_policy=UNRESTRICTED_HOST_POLICY)
        # The stale set rides out the failed walk (stale-read contract).
        assert _loc_raws(out) == ["http://x.test/p1"]

        # The good entry SURVIVED the failure: stale-readable, not displaced
        # by the empty failure result.
        cached = SITEMAP_CACHE.get_stale(key)
        assert cached is not None, "failure displaced the last good entry"
        assert _loc_raws(cached[0]) == ["http://x.test/p1"]
    finally:
        SITEMAP_CACHE.discard(key)
