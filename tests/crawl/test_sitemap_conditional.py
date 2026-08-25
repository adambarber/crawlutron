"""Sitemap fetches must send conditional requests and reuse the cache on 304 (#5)."""

import time

from app.crawl import fetch_helpers
from app.crawl.fetch_helpers import fetch_sitemap_urls, _SITEMAP_CACHE
from app.models import HttpFetchResult


def _resp(status, body="", etag=None, last_modified=None, ctype="application/xml"):
    headers = {"content-type": ctype}
    if etag:
        headers["etag"] = etag
    if last_modified:
        headers["last-modified"] = last_modified
    return HttpFetchResult(
        url="http://x.test/sitemap.xml", final_url="http://x.test/sitemap.xml",
        status_code=status, headers=headers, body=body.encode("utf-8"), elapsed_ms=1,
    )


async def test_304_reuses_cached_urls_without_reparsing():
    url = "http://x.test/sitemap.xml"
    # Seed an EXPIRED cache entry (forces a re-fetch attempt).
    _SITEMAP_CACHE[url] = (time.monotonic() - 7200, [("http://x.test/p1", None)], "v1", None)
    calls = []

    async def fetcher(u, headers, timeout, max_content_length=50 * 1024 * 1024):
        calls.append(headers)
        if headers.get("If-None-Match") == "v1":
            return _resp(304, etag="v1")  # not modified
        return _resp(200, "<x></x>", etag="v1")

    out = await fetch_sitemap_urls(fetcher, url, 10, conditional={"If-None-Match": "v1"})
    assert out == {("http://x.test/p1", None)}
    # Conditional header was sent.
    assert any(c.get("If-None-Match") == "v1" for c in calls)
    # Cache TTL was refreshed (entry still present, not re-parsed).
    assert _SITEMAP_CACHE[url][0] > time.monotonic() - 1


async def test_etag_captured_and_stored_on_200():
    url = "http://x.test/sitemap.xml"
    _SITEMAP_CACHE.pop(url, None)
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/p1</loc><lastmod>2024-01-01</lastmod></url>
    </urlset>"""

    async def fetcher(u, headers, timeout, max_content_length=50 * 1024 * 1024):
        return _resp(200, xml, etag="v2", last_modified="Wed, 01 Jan 2024 00:00:00 GMT")

    out = await fetch_sitemap_urls(fetcher, url, 10)
    assert ("http://x.test/p1", "2024-01-01") in out
    # The new etag/last-modified are stored so the next crawl can send a 304 check.
    assert _SITEMAP_CACHE[url][2] == "v2"
    assert _SITEMAP_CACHE[url][3] == "Wed, 01 Jan 2024 00:00:00 GMT"
