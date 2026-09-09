"""fetch_sitemap_urls must extract <lastmod> alongside <loc> (#1)."""

from app.crawl.fetch_helpers import fetch_sitemap_urls
from app.models import HttpFetchResult


def _sitemap_fetcher(xml: str):
    resp = HttpFetchResult(
        url="http://x.test/sitemap.xml", final_url="http://x.test/sitemap.xml",
        status_code=200, headers={"content-type": "application/xml"},
        body=xml.encode("utf-8"), elapsed_ms=1,
    )

    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        return resp

    return fetcher


async def test_extracts_lastmod_per_loc():
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/a</loc><lastmod>2024-01-15</lastmod></url>
      <url><loc>http://x.test/b</loc></url>
      <url><loc>http://x.test/c</loc><lastmod>2023-06-01T00:00:00Z</lastmod></url>
    </urlset>"""
    urls = await fetch_sitemap_urls(_sitemap_fetcher(xml), "http://x.test/sitemap.xml", 10)
    # returns (loc, lastmod) tuples
    by_loc = {loc: lastmod for loc, lastmod in urls}
    assert by_loc["http://x.test/a"] == "2024-01-15"
    assert by_loc["http://x.test/b"] is None
    assert by_loc["http://x.test/c"] == "2023-06-01T00:00:00Z"


async def test_index_children_recurse_without_lastmod():
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>http://x.test/sub.xml</loc></sitemap>
    </sitemapindex>"""
    sub = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/p</loc><lastmod>2024-03-03</lastmod></url>
    </urlset>"""

    def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        xml = index if url.endswith("/sitemap.xml") else sub
        return HttpFetchResult(
            url=url, final_url=url, status_code=200,
            headers={"content-type": "application/xml"},
            body=xml.encode("utf-8"), elapsed_ms=1,
        )

    urls = await fetch_sitemap_urls(fetcher, "http://x.test/sitemap.xml", 10)
    assert ("http://x.test/p", "2024-03-03") in urls
