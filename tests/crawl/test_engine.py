"""Engine orchestration behavior: scope filtering, conditional caching, fallback (no mocks)."""

import urllib.error

import pytest
from app.crawl.cache import HttpCache
from app.crawl.engine import _filter_links_by_scope, crawl_request
from app.crawl.strategies.browser import BrowserRenderer
from app.models import CrawlStrategyAttempt, DiscoveredLink, EngineCrawlResult
from app.constants import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER, EXTRACTION_FULL


def make_result(links):
    return EngineCrawlResult(
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


class TestScopeFilter:
    def test_scope_regex_filters_discovered_links(self):
        links = [
            DiscoveredLink(url="https://example.com/x", normalized_url="https://example.com/x", text="x", rel=[], source_url="https://example.com/", host="example.com", is_same_host=True),
            DiscoveredLink(url="https://other.com/y", normalized_url="https://other.com/y", text="y", rel=[], source_url="https://example.com/", host="other.com", is_same_host=False),
        ]
        filtered = _filter_links_by_scope(make_result(links), r"^https://example\.com")
        assert len(filtered.discovered_links) == 1
        assert filtered.discovered_links[0].url == "https://example.com/x"

    def test_none_scope_keeps_all(self):
        links = [DiscoveredLink(url="https://a.com/x", normalized_url="https://a.com/x", text="x", rel=[], source_url="https://a.com/", host="a.com", is_same_host=True)]
        assert _filter_links_by_scope(make_result(links), None).discovered_link_count == 1

    def test_invalid_scope_regex_keeps_all(self):
        links = [DiscoveredLink(url="https://a.com/x", normalized_url="https://a.com/x", text="x", rel=[], source_url="https://a.com/", host="a.com", is_same_host=True)]
        assert _filter_links_by_scope(make_result(links), r"([invalid").discovered_link_count == 1


class TestEngineCrawlRequest:
    async def test_crawl_request_http_success(self, base_url):
        from helpers import crawl_homepage_async
        response = await crawl_homepage_async(base_url)
        assert "Example Org" in response["results"][0]["markdown"]

    async def test_crawl_request_conditional_cache_304(self, base_url):
        from helpers import run_crawl_async
        url = f"{base_url}/with_etag"
        cache = HttpCache(":memory:")
        cache.store(url, {"etag": "v1"})
        payload = {
            "url": url,
            "policy": {
                "strategies": [HTTP_DECLARED_CRAWLER],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        response = await run_crawl_async(payload, cache=cache)
        result = response["results"][0]
        assert result["classification"] == "not_modified"
        assert result["http_status"] == 304

    async def test_crawl_request_browser_strategy_without_worker_is_browser_crash(self, base_url):
        from helpers import run_crawl_async
        payload = {
            "url": f"{base_url}/",
            "policy": {
                "strategies": [BROWSER_HEADLESS],
                "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
            },
        }
        response = await run_crawl_async(payload)
        result = response["results"][0]
        assert result["status"] == "failed"
        assert result["classification"] == "browser_crash"

    async def test_crawl_request_http_failure_falls_back_to_browser(self, base_url):
        from helpers import run_crawl_async

        def failing_fetcher(target_url, headers, timeout, max_content_length=50 * 1024 * 1024):
            raise urllib.error.URLError("connection refused")

        renderer = BrowserRenderer()

        async def browser_worker(job):
            return await renderer.render(job.url, job.options)

        payload = {
            "url": f"{base_url}/",
            "policy": {
                "strategies": [HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS],
                "limits": {"timeout_seconds": 20, "max_retries": 0, "max_discovered_links": 50},
            },
        }
        response = await crawl_request(
            payload, browser_worker=browser_worker, http_fetcher=failing_fetcher, cache=HttpCache()
        )
        result = response["results"][0]
        assert result["status"] == "success"
        assert result["classification"] == "rendered"
        assert "Example Org" in result["markdown"]
