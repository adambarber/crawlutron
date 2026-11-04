"""Cold sitemap fetches dedupe through single-flight (#498)."""

import asyncio

from crawlutron.transport.fetch_helpers import SITEMAP_CACHE, fetch_sitemap_urls
from tests._fetch_doubles import FetcherDouble
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

    # One upstream walk is the property: the fetcher counts entries and never lets the second caller start a
    # second one, so the counter's high water is the number of walks that ran.
    _walks = {"n": 0, "most": 0}

    async def _shared_walk_answer(url, sent_headers, *_args):
        _walks["n"] += 1
        _walks["most"] = max(_walks["most"], _walks["n"])
        try:
            remaining = 5
            while remaining:
                await asyncio.sleep(0)
                remaining -= 1
            return slow_respond(url, sent_headers)
        finally:
            _walks["n"] -= 1

    fetcher = FetcherDouble(_shared_walk_answer)
    fetcher.peak = lambda: _walks["most"]

    first, second = await asyncio.gather(
        fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY),
        fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY),
    )

    expected = {(canonical("http://x.test/p1"), None)}
    assert first == expected
    assert second == expected
    assert fetcher.peak() == 1, "both callers walked the sitemap upstream independently"
