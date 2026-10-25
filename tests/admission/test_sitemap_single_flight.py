"""Cold sitemap fetches dedupe through single-flight (#498)."""

import asyncio

from app.orchestration.fetch_helpers import SITEMAP_CACHE, fetch_sitemap_urls
from tests._fetcher_factories import concurrent_fetcher
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical


async def test_concurrent_cold_fetches_share_one_upstream_walk():
    """Two parallel cold fetches of one sitemap must produce one upstream walk."""
    url = canonical("http://x.test/sitemap.xml")
    SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
    body = (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>http://x.test/p1</loc></url></urlset>"
    )

    fetch_started = asyncio.Event()

    def slow_respond(u, headers):
        fetch_started.set()
        return (200, body.encode(), "application/xml")

    fetcher = concurrent_fetcher(slow_respond, yield_times=5)

    first, second = await asyncio.gather(
        fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY),
        fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY),
    )

    expected = {(canonical("http://x.test/p1"), None)}
    assert first == expected
    assert second == expected
    assert fetcher.peak() == 1, "both callers walked the sitemap upstream independently"
