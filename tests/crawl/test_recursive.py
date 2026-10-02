"""Bounded recursive crawl, robots filtering, sitemap seeding, and memory-strip.

Uses a deterministic in-process fake fetcher (no real network I/O) plus one
real-server smoke test.
"""

from tests._fetcher_factories import (
    LINKED_PAIR_LIMITS,
    crawled_results,
    crawled_urls,
    run_scoped_crawl,
    site_linking_to,
    xml_page,
)


def _assert_memory_stripped(out) -> None:
    """Every result must have dropped html/text but kept its markdown.

    The non-empty check is the half that matters: the loop below passes
    vacuously on an empty result list, which is what a crawl that found nothing
    hands back.
    """
    assert out["results"], "the crawl returned nothing, so nothing was stripped"
    for result in out["results"]:
        assert result.html is None
        assert result.text is None
        assert result.markdown is not None


class TestRecursiveCrawl:
    async def test_max_depth_one_discovers_links(self):
        pages = site_linking_to("/a", "/b")
        out = await crawled_results(pages, limits=LINKED_PAIR_LIMITS)
        assert out["submitted_count"] == 1
        urls = {r.normalized_url for r in out["results"]}
        assert "http://example.com" in urls
        assert "http://example.com/a" in urls
        assert "http://example.com/b" in urls

    async def test_max_pages_bounds_recursion(self):
        pages = site_linking_to("/a", "/b", "/c")
        out = await crawled_results(pages, limits={"max_depth": 2, "max_pages": 2})
        assert len(out["results"]) <= 2

    async def test_wire_edge_bounds_an_absent_or_zero_cap_before_the_crawl_sees_it(self):
        """0 means "unbounded" to a stored policy, but the API substitutes a budget.

        Two layers read 0 as unbounded: ``remaining_page_capacity`` returns None
        for it, and a whole-site job read back from its row keeps the 0. The API
        edge is deliberately different -- an externally-triggered crawl must be
        bounded -- and substitutes the default page budget. This pins that
        substitution where a test can see it end to end.

        It matters because the dispatch loop has its own, different reading of 0,
        asserted directly in ``test_recursive_dispatch``. Without a test on this
        substitution, deleting it would not fail here; the loop would just
        quietly receive 0 and crawl nothing.
        """
        from app.api_models import CrawlPolicy as PolicyModel
        from app.crawl.policy import validate_wire_policy

        assert PolicyModel.model_fields["limits"].annotation.model_fields["max_pages"].default == 0, (
            "the model default is the 'unbounded' sentinel this edge substitutes"
        )
        assert validate_wire_policy({"limits": {"max_pages": 0}}).limits.max_pages > 0

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
        urls = set(
            await crawled_urls(pages, limits={"max_depth": 1, "max_pages": 10, "respect_robots": True})
        )
        assert "http://example.com/public" in urls
        assert "http://example.com/private" not in urls

    async def test_robots_disallows_seed_url(self):
        pages = {
            "/robots.txt": (200, "User-agent: *\nDisallow: /seed\n", "text/plain"),
            "/": (200, "<html>home</html>", "text/html"),
            "/seed": (200, "<html>seed</html>", "text/html"),
        }
        out = await crawled_results(
            pages, url="http://example.com/seed", limits={"max_depth": 0, "respect_robots": True}
        )
        result = out["results"][0]
        assert result.classification == "robots_disallowed"
        assert result.status == "skipped"

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
        urls = set(
            await crawled_urls(pages, limits=LINKED_PAIR_LIMITS, policy={"discovery": {"sitemaps": True}})
        )
        assert "http://example.com/s1" in urls
        assert "http://example.com/s2" in urls

    async def test_memory_strip_when_include_html_false(self):
        pages = site_linking_to("/a", bodies={"/a": "<html><body>hello</body></html>"})
        out = await crawled_results(
            pages,
            limits=LINKED_PAIR_LIMITS,
            policy={"browser_profile": {"include_html": False}},
        )
        _assert_memory_stripped(out)


class TestRecursiveCrawlServer:
    async def test_real_server_recursive_follows_links(self, base_url):
        from tests._fetcher_factories import run_crawl_async

        payload = {
            "url": f"{base_url}/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "limits": {"max_depth": 1, "max_pages": 10},
            },
        }
        response = await run_crawl_async(payload)
        urls = {r.normalized_url for r in response["results"]}
        assert base_url in urls
        assert base_url + "/page2" in urls

    async def test_memory_strip_both_modes_include_html_false(self):
        # html/text must be stripped inside the loop for both the recursive and
        # non-recursive branches, not retained for the whole crawl.
        pages = site_linking_to("/a", bodies={"/a": "<html><body>hello</body></html>"})

        async def run(max_depth):
            return await crawled_results(
                pages,
                limits={"max_depth": max_depth, "max_pages": 10},
                policy={"browser_profile": {"include_html": False}},
            )

        for max_depth in (0, 1):
            _assert_memory_stripped(await run(max_depth))


async def test_real_sitemap_parser_feeds_scope_filtered_recursive_crawl():
    pages = {
        "/": (200, "<html>home</html>", "text/html"),
        "/sitemap.xml": xml_page(
            "<sitemapindex><sitemap><loc>https://example.com/child-sitemap.xml</loc></sitemap></sitemapindex>"
        ),
        "/child-sitemap.xml": xml_page(
            "<urlset>"
            "<url><loc>https://example.com/keep?utm_source=test</loc></url>"
            "<url><loc>https://example.com/keep/</loc></url>"
            "<url><loc>https://example.com/drop</loc></url>"
            "</urlset>"
        ),
        "/keep": (200, "<html>keep</html>", "text/html"),
        "/drop": (200, "<html>drop</html>", "text/html"),
    }
    normalized = await crawled_urls(
        pages,
        url="https://example.com/",
        limits=LINKED_PAIR_LIMITS,
        policy={"discovery": {"sitemaps": True}, "scope_regex": r"^https://example\.com/keep/?$"},
    )

    assert normalized.count("https://example.com/keep") == 1
    assert "https://example.com/drop" not in normalized
