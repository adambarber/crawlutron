"""Engine orchestration behavior: scope filtering, conditional caching, fallback (no mocks)."""

import urllib.error

import pytest
from _testkit import browser_available

from app.constants import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.browser import BrowserRenderer
from app.models import CrawlStrategyAttempt, EngineCrawlResult


def make_result(links):
    return EngineCrawlResult(
        original_url="https://example.com/",
        normalized_url="https://example.com/",
        final_url="https://example.com/",
        status="success",
        classification="rendered",
        selected_strategy=HTTP_DECLARED_CRAWLER,
        attempted_strategies=[
            CrawlStrategyAttempt(
                strategy=HTTP_DECLARED_CRAWLER,
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=1,
            )
        ],
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


class TestEngineCrawlRequest:
    async def test_crawl_request_http_success(self, base_url):
        from _testkit import crawl_homepage_async

        response = await crawl_homepage_async(base_url)
        assert "Example Org" in response["results"][0]["markdown"]

    async def test_crawl_request_conditional_cache_304(self, base_url):
        from _testkit import run_crawl_async

        url = f"{base_url}/with_etag"
        cache = HttpCache()
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
        from _testkit import run_crawl_async

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

    @pytest.mark.browser
    @pytest.mark.skipif(not browser_available(), reason="no usable browser binary (camoufox/playwright)")
    async def test_crawl_request_http_failure_falls_back_to_browser(self, base_url):

        class _FakeRobotsOk:
            status_code = 200
            headers = {}
            body = b"User-agent: *\nAllow: /\n"

        def failing_fetcher(target_url, headers, timeout, max_content_length=50 * 1024 * 1024):
            # Fail page fetches but serve robots.txt: robots is per-origin
            # (M7 fail-closed), so a fake that also breaks robots would
            # skip the crawl before the browser fallback is ever reached.
            # The intent here is page-fetch failure -> browser fallback.
            if target_url.endswith("/robots.txt"):
                return _FakeRobotsOk()
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


def test_seed_freshness_uses_real_default_strategies():
    """L5b: without an explicit strategies list, freshness must probe the
    real default chain -- not the placeholder ["HTTP"], whose cache keys
    can never match anything actually stored."""
    from app.constants import DEFAULT_EXTRACTION, HTTP_DECLARED_CRAWLER
    from app.crawl.engine import _parse_lastmod, _seed_is_fresh
    from app.crawl.strategies.http import _content_cache_key, _headers_for_strategy

    url = "http://seed-fresh.test/page"
    # Normalized shape but no strategies key: the default must apply.
    policy = {"headers": {}, "request_profile": {}}
    expected_key = _content_cache_key(
        url,
        HTTP_DECLARED_CRAWLER,
        _headers_for_strategy(HTTP_DECLARED_CRAWLER, policy, url),
        DEFAULT_EXTRACTION,
        None,
        None,
    )
    cached_at = _parse_lastmod("2024-06-01")

    class _StubCache:
        def cached_at_of(self, key):
            return cached_at if key == expected_key else None

    assert _seed_is_fresh(url, "2024-01-01", _StubCache(), policy) is True


class TestCrawlRequestWebhook:
    async def test_engine_crawl_request_with_webhook(self):
        import asyncio

        from app.crawl.engine import crawl_request
        from app.settings import CrawlerSettings

        payload = {
            "url": "http://127.0.0.1:1/",
            "policy": {
                "strategies": ["HTTP_DECLARED_CRAWLER"],
                "extraction": "full",
                "headers": {},
                "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 5},
                "request_profile": {},
            },
            "webhook_url": "http://webhook.invalid",
        }

        async def browser_worker(job):
            return None

        result = await crawl_request(
            payload,
            settings=CrawlerSettings.from_mapping(),
            browser_worker=browser_worker,
            shutdown_event=asyncio.Event(),
            webhook_url="http://webhook.invalid",
        )
        assert "webhook_url" in result
