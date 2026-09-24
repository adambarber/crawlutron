"""Bounded recursive crawl, robots filtering, sitemap seeding, and memory-strip.

Uses a deterministic in-process fake fetcher (no real network I/O) plus one
real-server smoke test.
"""

from _fetcher_factories import run_scoped_crawl, site_fetcher

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request


class TestRecursiveCrawl:
    async def test_max_depth_one_discovers_links(self):
        pages = {
            "/": (200, '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>', "text/html"),
            "/a": (200, "<html><body>a</body></html>", "text/html"),
            "/b": (200, "<html><body>b</body></html>", "text/html"),
        }
        payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1, "max_pages": 10}}}
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        assert out["submitted_count"] == 1
        urls = {r["normalized_url"] for r in out["results"]}
        assert "http://example.com" in urls
        assert "http://example.com/a" in urls
        assert "http://example.com/b" in urls

    async def test_max_pages_bounds_recursion(self):
        pages = {
            "/": (
                200,
                '<html><body><a href="/a">a</a><a href="/b">b</a><a href="/c">c</a></body></html>',
                "text/html",
            ),
            "/a": (200, "<html>a</html>", "text/html"),
            "/b": (200, "<html>b</html>", "text/html"),
            "/c": (200, "<html>c</html>", "text/html"),
        }
        payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 2, "max_pages": 2}}}
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        assert len(out["results"]) <= 2

    async def test_scope_regex_limits_discovered_links(self):
        urls = await run_scoped_crawl(r"/keep/")
        assert "http://example.com/keep/x" in urls
        assert "http://example.com/drop/y" not in urls

    async def test_robots_disallows_link(self):
        pages = {
            "/robots.txt": (200, "User-agent: *\nDisallow: /private\n", "text/plain"),
            "/": (
                200,
                '<html><body><a href="/public">pub</a><a href="/private">priv</a></body></html>',
                "text/html",
            ),
            "/public": (200, "<html>pub</html>", "text/html"),
            "/private": (200, "<html>priv</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10, "respect_robots": True}},
        }
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        urls = {r["normalized_url"] for r in out["results"]}
        assert "http://example.com/public" in urls
        assert "http://example.com/private" not in urls

    async def test_robots_disallows_seed_url(self):
        pages = {
            "/robots.txt": (200, "User-agent: *\nDisallow: /seed\n", "text/plain"),
            "/": (200, "<html>home</html>", "text/html"),
            "/seed": (200, "<html>seed</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/seed",
            "policy": {"limits": {"max_depth": 0, "respect_robots": True}},
        }
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        result = out["results"][0]
        assert result["classification"] == "robots_disallowed"
        assert result["status"] == "skipped"

    async def test_sitemap_seeding(self):
        pages = {
            "/": (200, "<html>home</html>", "text/html"),
            "/sitemap.xml": (
                200,
                "<urlset><url><loc>http://example.com/s1</loc></url><url><loc>http://example.com/s2</loc></url></urlset>",
                "application/xml",
            ),
            "/s1": (200, "<html>s1</html>", "text/html"),
            "/s2": (200, "<html>s2</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "discovery": {"sitemaps": True}},
        }
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        urls = {r["normalized_url"] for r in out["results"]}
        assert "http://example.com/s1" in urls
        assert "http://example.com/s2" in urls

    async def test_memory_strip_when_include_html_false(self):
        pages = {
            "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
            "/a": (200, "<html><body>hello</body></html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {
                "limits": {"max_depth": 1, "max_pages": 10},
                "browser_profile": {"include_html": False},
            },
        }
        out = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
        for result in out["results"]:
            assert result["html"] is None
            assert result["text"] is None
            assert result["markdown"] is not None


class TestRecursiveCrawlServer:
    async def test_real_server_recursive_follows_links(self, base_url):
        from _fetcher_factories import run_crawl_async

        payload = {
            "url": f"{base_url}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"max_depth": 1, "max_pages": 10},
            },
        }
        response = await run_crawl_async(payload)
        urls = {r["normalized_url"] for r in response["results"]}
        assert base_url in urls
        assert base_url + "/page2" in urls

    async def test_memory_strip_both_modes_include_html_false(self):
        # html/text must be stripped inside the loop for both the recursive and
        # non-recursive branches (#1), not retained for the whole crawl.
        pages = {
            "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
            "/a": (200, "<html><body>hello</body></html>", "text/html"),
        }

        async def run(max_depth):
            payload = {
                "url": "http://example.com/",
                "policy": {
                    "limits": {"max_depth": max_depth, "max_pages": 10},
                    "browser_profile": {"include_html": False},
                },
            }
            return await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())

        for max_depth in (0, 1):
            out = await run(max_depth)
            for result in out["results"]:
                assert result["html"] is None
                assert result["text"] is None
                assert result["markdown"] is not None


async def test_real_sitemap_parser_feeds_scope_filtered_recursive_crawl():
    pages = {
        "/": (200, "<html>home</html>", "text/html"),
        "/sitemap.xml": (
            200,
            "<sitemapindex><sitemap><loc>https://example.com/child-sitemap.xml</loc></sitemap></sitemapindex>",
            "application/xml",
        ),
        "/child-sitemap.xml": (
            200,
            "<urlset>"
            "<url><loc>https://example.com/keep?utm_source=test</loc></url>"
            "<url><loc>https://example.com/keep/</loc></url>"
            "<url><loc>https://example.com/drop</loc></url>"
            "</urlset>",
            "application/xml",
        ),
        "/keep": (200, "<html>keep</html>", "text/html"),
        "/drop": (200, "<html>drop</html>", "text/html"),
    }
    payload = {
        "url": "https://example.com/",
        "policy": {
            "limits": {"max_depth": 1, "max_pages": 10},
            "discovery": {"sitemaps": True},
            "scope_regex": r"^https://example\.com/keep/?$",
        },
    }

    output = await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache())
    normalized = [result["normalized_url"] for result in output["results"]]

    assert normalized.count("https://example.com/keep") == 1
    assert "https://example.com/drop" not in normalized
