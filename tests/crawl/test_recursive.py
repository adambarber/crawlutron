"""Bounded recursive crawl, robots filtering, sitemap seeding, and memory-strip.

Uses a deterministic in-process fake fetcher (no real network I/O) plus one
real-server smoke test.
"""

import asyncio
import unittest
from urllib.parse import urlparse

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.http import HttpFetchResult
from helpers import ServerTestCase


def site_fetcher(pages):
    """Build a sync fetcher serving an in-memory ``path -> (status, body, ctype)`` map."""

    def fetcher(url, headers, timeout):
        path = urlparse(url).path or "/"
        entry = pages.get(path)
        if entry is None:
            return HttpFetchResult(
                url=url, final_url=url, status_code=404,
                headers={"content-type": "text/html"}, body=b"<html></html>", elapsed_ms=1,
            )
        status, body, ctype = entry
        if isinstance(body, str):
            body = body.encode("utf-8")
        return HttpFetchResult(
            url=url, final_url=url, status_code=status,
            headers={"content-type": ctype}, body=body, elapsed_ms=1,
        )

    return fetcher


class RecursiveCrawlTest(unittest.TestCase):
    def test_max_depth_one_discovers_links(self):
        pages = {
            "/": (200, '<html><body><a href="/a">a</a><a href="/b">b</a></body></html>', "text/html"),
            "/a": (200, "<html><body>a</body></html>", "text/html"),
            "/b": (200, "<html><body>b</body></html>", "text/html"),
        }
        payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1, "max_pages": 10}}}
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        self.assertEqual(out["submitted_count"], 1)
        urls = {r["normalized_url"] for r in out["results"]}
        self.assertIn("http://example.com", urls)
        self.assertIn("http://example.com/a", urls)
        self.assertIn("http://example.com/b", urls)

    def test_max_pages_bounds_recursion(self):
        pages = {
            "/": (200, '<html><body><a href="/a">a</a><a href="/b">b</a><a href="/c">c</a></body></html>', "text/html"),
            "/a": (200, "<html>a</html>", "text/html"),
            "/b": (200, "<html>b</html>", "text/html"),
            "/c": (200, "<html>c</html>", "text/html"),
        }
        payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 2, "max_pages": 2}}}
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        self.assertLessEqual(len(out["results"]), 2)

    def test_scope_regex_limits_discovered_links(self):
        pages = {
            "/": (200, '<html><body><a href="/keep/x">k</a><a href="/drop/y">d</a></body></html>', "text/html"),
            "/keep/x": (200, "<html>k</html>", "text/html"),
            "/drop/y": (200, "<html>d</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "scope_regex": r"/keep/"},
        }
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        urls = {r["normalized_url"] for r in out["results"]}
        self.assertIn("http://example.com/keep/x", urls)
        self.assertNotIn("http://example.com/drop/y", urls)

    def test_robots_disallows_link(self):
        pages = {
            "/robots.txt": (200, "User-agent: *\nDisallow: /private\n", "text/plain"),
            "/": (200, '<html><body><a href="/public">pub</a><a href="/private">priv</a></body></html>', "text/html"),
            "/public": (200, "<html>pub</html>", "text/html"),
            "/private": (200, "<html>priv</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10, "respect_robots": True}},
        }
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        urls = {r["normalized_url"] for r in out["results"]}
        self.assertIn("http://example.com/public", urls)
        self.assertNotIn("http://example.com/private", urls)

    def test_robots_disallows_seed_url(self):
        pages = {
            "/robots.txt": (200, "User-agent: *\nDisallow: /seed\n", "text/plain"),
            "/": (200, "<html>home</html>", "text/html"),
            "/seed": (200, "<html>seed</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/seed",
            "policy": {"limits": {"max_depth": 0, "respect_robots": True}},
        }
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        result = out["results"][0]
        self.assertEqual(result["classification"], "robots_disallowed")
        self.assertEqual(result["status"], "skipped")

    def test_sitemap_seeding(self):
        pages = {
            "/": (200, "<html>home</html>", "text/html"),
            "/sitemap.xml": (
                200,
                '<urlset><url><loc>http://example.com/s1</loc></url><url><loc>http://example.com/s2</loc></url></urlset>',
                "application/xml",
            ),
            "/s1": (200, "<html>s1</html>", "text/html"),
            "/s2": (200, "<html>s2</html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "discovery": {"sitemaps": True}},
        }
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        urls = {r["normalized_url"] for r in out["results"]}
        self.assertIn("http://example.com/s1", urls)
        self.assertIn("http://example.com/s2", urls)

    def test_memory_strip_when_include_html_false(self):
        pages = {
            "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
            "/a": (200, "<html><body>hello</body></html>", "text/html"),
        }
        payload = {
            "url": "http://example.com/",
            "policy": {"limits": {"max_depth": 1, "max_pages": 10}, "browser_profile": {"include_html": False}},
        }
        out = asyncio.run(crawl_request(payload, http_fetcher=site_fetcher(pages), cache=HttpCache()))
        for result in out["results"]:
            self.assertIsNone(result["html"])
            self.assertIsNone(result["text"])
            self.assertIsNotNone(result["markdown"])


class RecursiveCrawlServerTest(ServerTestCase):
    def test_real_server_recursive_follows_links(self):
        payload = self.make_payload(
            url=f"{self.base}/",
            strategies=["HTTP_DECLARED_CRAWLER"],
            limits={"max_depth": 1, "max_pages": 10},
        )
        response = self.run_crawl(payload)
        urls = {r["normalized_url"] for r in response["results"]}
        self.assertIn(self.base, urls)
        self.assertIn(self.base + "/page2", urls)


if __name__ == "__main__":
    unittest.main()
