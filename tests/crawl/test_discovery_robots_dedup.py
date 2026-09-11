"""Discovery diagnostics must reuse robots Sitemap directives passed from the
engine instead of re-fetching /robots.txt (#4)."""

from _testkit import make_fetch

from app.crawl.strategies.http import _discovery_diagnostics


def _resp(url, body, ctype):
    return make_fetch(url=url, final_url=url, status=200, ctype=ctype, body=body.encode("utf-8"))


async def _fake_fetcher(calls, url, headers, timeout, max_content_length=50 * 1024 * 1024):
    calls.append(url)
    if url.endswith("/robots.txt"):
        return _resp(url, "User-agent: *\nDisallow: /secret\n", "text/plain")
    if "sitemap" in url:
        return _resp(
            url,
            "<urlset><loc>https://x.test/p1</loc><lastmod>2024-05-05</lastmod></urlset>",
            "application/xml",
        )
    return _resp(url, "<html></html>", "text/html")


async def _run_diag(calls, *, engine_robots_sitemaps):
    return await _discovery_diagnostics(
        normalized_url="https://x.test/p",
        html="<html></html>",
        policy={"discovery": {"robots": True, "sitemaps": True}, "limits": {"timeout_seconds": 10}},
        headers={},
        http_fetcher=lambda *a, **k: _fake_fetcher(calls, *a, **k),
        engine_robots_sitemaps=engine_robots_sitemaps,
    )


async def test_discovery_skips_robots_fetch_when_sitemaps_passed():
    calls = []
    diag = await _run_diag(calls, engine_robots_sitemaps=["https://x.test/sitemap.xml"])
    # robots.txt must NOT be fetched again; the engine-provided sitemaps are used.
    assert "https://x.test/robots.txt" not in calls
    # sitemap still fetched + parsed
    assert "https://x.test/sitemap.xml" in calls
    assert diag["discovery"]["sitemap_urls"] == ["https://x.test/p1"]
    # lastmod is surfaced for downstream skip-if-unchanged logic (#1)
    assert diag.get("sitemap_lastmod") == {"https://x.test/p1": "2024-05-05"}


async def test_discovery_fetches_robots_when_no_sitemaps_passed():
    calls = []
    diag = await _run_diag(calls, engine_robots_sitemaps=None)
    # Without engine-provided sitemaps, robots.txt is fetched as before.
    assert "https://x.test/robots.txt" in calls
    assert diag["discovery"]["sitemap_urls"] == ["https://x.test/p1"]
