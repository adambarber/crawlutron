"""Engine orchestration behavior: scope filtering, conditional caching, fallback (no mocks)."""

import asyncio
import urllib.error
import unittest

from app.crawl.cache import HttpCache
from app.crawl.engine import _filter_links_by_scope, crawl_request
from app.crawl.strategies.browser import BrowserRenderer
from app.models import CrawlStrategyAttempt, DiscoveredLink, UniversalCrawlResult
from helpers import ServerTestCase
from app.constants import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER, EXTRACTION_FULL


def make_result(links):
    return UniversalCrawlResult(
        original_url="https://example.com/",
        normalized_url="https://example.com/",
        final_url="https://example.com/",
        status="success",
        classification="rendered",
        selected_strategy=HTTP_DECLARED_CRAWLER,
        attempted_strategies=[CrawlStrategyAttempt(strategy=HTTP_DECLARED_CRAWLER, status="success", classification="rendered", http_status=200, content_type="text/html", elapsed_ms=1)],
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text="x",
        extraction_method="http_html",
        quality_score=1.0,
        discovered_link_count=len(links),
        discovered_links=links,
    )


class ScopeFilterTest(unittest.TestCase):
    def test_scope_regex_filters_discovered_links(self) -> None:
        links = [
            DiscoveredLink(url="https://example.com/x", normalized_url="https://example.com/x", text="x", rel=[], source_url="https://example.com/", host="example.com", is_same_host=True),
            DiscoveredLink(url="https://other.com/y", normalized_url="https://other.com/y", text="y", rel=[], source_url="https://example.com/", host="other.com", is_same_host=False),
        ]
        filtered = _filter_links_by_scope(make_result(links), r"^https://example\.com")
        self.assertEqual(len(filtered.discovered_links), 1)
        self.assertEqual(filtered.discovered_links[0].url, "https://example.com/x")

    def test_none_scope_keeps_all(self) -> None:
        links = [DiscoveredLink(url="https://a.com/x", normalized_url="https://a.com/x", text="x", rel=[], source_url="https://a.com/", host="a.com", is_same_host=True)]
        self.assertEqual(_filter_links_by_scope(make_result(links), None).discovered_link_count, 1)

    def test_invalid_scope_regex_keeps_all(self) -> None:
        links = [DiscoveredLink(url="https://a.com/x", normalized_url="https://a.com/x", text="x", rel=[], source_url="https://a.com/", host="a.com", is_same_host=True)]
        self.assertEqual(_filter_links_by_scope(make_result(links), r"([invalid").discovered_link_count, 1)


class EngineCrawlRequestTest(ServerTestCase):
    def test_crawl_request_http_success(self) -> None:
        payload = self.make_payload(
            strategies=[HTTP_DECLARED_CRAWLER],
            limits={"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
        )
        response = self.run_crawl(payload)
        self.assertIn("Example Org", response["results"][0]["markdown"])

    def test_crawl_request_conditional_cache_304(self) -> None:
        url = f"{self.base}/with_etag"
        cache = HttpCache(":memory:")
        cache.store(url, {"etag": "v1"})
        payload = self.make_payload(
            url=url,
            strategies=[HTTP_DECLARED_CRAWLER],
            limits={"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
        )
        response = self.run_crawl(payload, cache=cache)
        result = response["results"][0]
        self.assertEqual(result["classification"], "not_modified")
        self.assertEqual(result["http_status"], 304)

    def test_crawl_request_browser_strategy_without_worker_is_browser_crash(self) -> None:
        payload = self.make_payload(
            strategies=[BROWSER_HEADLESS],
            limits={"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
        )
        response = self.run_crawl(payload)
        result = response["results"][0]
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["classification"], "browser_crash")

    def test_crawl_request_http_failure_falls_back_to_browser(self) -> None:
        def failing_fetcher(target_url, headers, timeout):
            raise urllib.error.URLError("connection refused")

        renderer = BrowserRenderer()

        def browser_worker(job):
            return renderer.render(job.url, job.options)

        payload = self.make_payload(
            strategies=[HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS],
            limits={"timeout_seconds": 20, "max_retries": 0, "max_discovered_links": 50},
        )
        response = asyncio.run(
            crawl_request(payload, browser_worker=browser_worker, http_fetcher=failing_fetcher, cache=HttpCache())
        )
        result = response["results"][0]
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["classification"], "rendered")
        self.assertIn("Example Org", result["markdown"])


if __name__ == "__main__":
    unittest.main()
