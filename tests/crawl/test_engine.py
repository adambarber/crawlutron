"""Crawl engine: strategy dispatch, chain edges and scope resolution."""

import urllib.error

import pytest
from _testkit import *
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


class TestEngineChainEdges:
    # Uses the module-level _fake_browser worker factory.

    def test_result_dict(self):
        d = to_dict(
            CrawlResult(
                url="http://x/",
                status="success",
                classification="rendered",
                final_url="http://x/",
                html=None,
                text="t",
                attempts=1,
                elapsed_ms=1,
                domain="x",
                metadata={},
            )
        )
        assert d["url"] == "http://x/"

    def test_payload_policy_invalid_strategy(self):
        with pytest.raises(ValueError):
            _payload_policy({"policy": {"strategies": ["BOGUS"]}}, None)

    async def test_browser_strategy_document_extraction(self, base_url):
        # Live origin: M7's robots gate fail-closes connection-refused origins
        # (127.0.0.1:1), so the subject here needs a server that 404s robots.
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT}
        result = await crawl_request(
            {"url": f"{base_url}/doc", "policy": policy},
            browser_worker=_fake_browser(html="<html><body>doc table</body></html>"),
            cache=HttpCache(),
        )
        assert result["results"][0]["status"] == "success"
        assert "doc table" in result["results"][0]["markdown"] or ""

    async def test_browser_strategy_trafilatura_extraction(self, base_url):
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA}
        result = await crawl_request(
            {"url": f"{base_url}/doc2", "policy": policy},
            browser_worker=_fake_browser(html="<html><body>trafilatura body</body></html>"),
            cache=HttpCache(),
        )
        assert "trafilatura body" in result["results"][0]["markdown"] or ""

    async def test_browser_strategy_no_content(self, base_url):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = await crawl_request(
            {"url": f"{base_url}/doc3", "policy": policy},
            browser_worker=_fake_browser(html=None, text=None),
            cache=HttpCache(),
        )
        assert (result["results"][0]["markdown"]) is None

    async def test_browser_strategy_surfaces_xhr_and_screenshot(self, base_url):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = await crawl_request(
            {"url": f"{base_url}/doc4", "policy": policy},
            browser_worker=_fake_browser(
                html="<html><body>x</body></html>",
                metadata={"intercepted_xhr": [{"url": "u", "payload": {}}], "screenshot": "BASE64IMG"},
            ),
            cache=HttpCache(),
        )
        diag = result["results"][0]["diagnostics"]
        assert "intercepted_xhr" in diag
        assert "screenshot" in diag

    async def test_per_url_budget_fails_slow_chain(self):
        """Adversarial perf #2.2: a URL that blows its total_url_budget_seconds
        must be failed as network_error instead of holding a slot."""
        import asyncio

        async def slow_fetcher(url, headers, timeout_seconds, max_content_length=50 * 1024 * 1024):
            # True tarpit: never returns until the caller's deadline cancels us.
            # (A time-based sleep is monkeypatched to return instantly in this
            # suite's fast mode, so model the poisoned-URL hang with an
            # ever-pending future instead.)
            await asyncio.Future()

        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "extraction": "dom",
            "limits": {
                "timeout_seconds": 30,
                "max_retries": 0,
                "max_discovered_links": 10,
                "respect_robots": False,
                "total_url_budget_seconds": 0.1,
            },
            "headers": {},
            "request_profile": {},
        }
        response = await crawl_request(
            {"url": "http://example.com/slow", "policy": policy},
            http_fetcher=slow_fetcher,
            cache=HttpCache(),
        )
        result = response["results"][0]
        assert result["status"] == "failed"
        # The budget's classification is precise (url_budget_exceeded), not the
        # generic network_error tail the old hand-built result hard-coded.
        assert result["classification"] == "url_budget_exceeded"
        assert result["error"] == "url_budget_exceeded"

    async def test_rate_limited_sets_domain_cooldown(self):
        from app.scheduler import DomainScheduler

        class _Resp:
            status_code = 429
            final_url = "http://example.com/"
            elapsed_ms = 1
            headers = {"retry-after": "30", "content-type": "text/plain"}
            body = b"rate limited"

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            return _Resp()

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "extraction": "dom",
            "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            "headers": {},
            "request_profile": {},
        }
        result = await _execute_crawl_chain(
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            policy=policy,
            http_fetcher=fetcher,
            cffi_fetcher=fetcher,
            browser_worker=None,
            scheduler=scheduler,
            domain="example.com",
        )
        assert result.classification == "rate_limited"
        assert "example.com" in scheduler._domain_cooldowns
        import time as _time

        assert abs(scheduler._domain_cooldowns["example.com"] - (_time.time() + 30)) <= 5

    async def test_robots_crawl_delay_sets_domain_min_delay(self, monkeypatch):
        """Adversarial perf #2.6: a host's robots Crawl-delay is honored as a
        per-domain minimum delay during the crawl."""
        from app.crawl import engine as engine_mod
        from app.crawl.cache import HttpCache
        from app.scheduler import DomainScheduler

        calls = []
        orig = DomainScheduler.set_domain_min_delay

        def spy(self, domain, seconds):
            calls.append((domain, seconds))
            return orig(self, domain, seconds)

        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", spy)

        def fetcher(url, headers, timeout_seconds, max_content_length=50 * 1024 * 1024):

            if url.endswith("/robots.txt"):
                body = b"User-agent: *\nCrawl-delay: 2\n"
            else:
                body = b"<html><body>ok</body></html>"
            return make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=body)

        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "extraction": "dom",
            "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
        }
        await engine_mod.crawl_request(
            {"url": "http://example.com/", "policy": policy},
            http_fetcher=fetcher,
            cache=HttpCache(),
        )
        assert any(d == "example.com" and abs(s - 2.0) < 1e-6 for d, s in calls), calls

    def test_effective_max_content_length_scales(self):
        """Adversarial perf #2.1: per-page cap shrinks as concurrency rises, floor
        respected, explicit policy value wins."""
        from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES
        from app.crawl.strategies.http import _effective_max_content_length

        # Explicit value always wins.
        assert _effective_max_content_length({"max_content_length_bytes": 123}) == 123
        # Default concurrency (4) -> unchanged 10 MiB default.
        assert _effective_max_content_length({}) == DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # High concurrency shrinks the cap.
        small = _effective_max_content_length({}, global_concurrency=400)
        assert small < DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # Floor is respected even at absurd concurrency.
        assert small >= 256 * 1024
        assert _effective_max_content_length({}, global_concurrency=100000) == 256 * 1024

    async def test_include_html_false_drops_html(self, monkeypatch):
        """When include_html is False the raw HTML is not retained in the result."""
        from app.crawl import engine as engine_mod
        from app.crawl.cache import HttpCache

        def fetcher(url, headers, timeout_seconds, max_content_length=50 * 1024 * 1024):
            body = b"<html><body>some page content</body></html>"
            return make_fetch(url=url, final_url=url, status=200, ctype="text/html", body=body)

        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "extraction": "dom",
            "browser_profile": {"include_html": False},
            "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 10},
        }
        out = await engine_mod.crawl_request(
            {"url": "http://example.com/", "policy": policy},
            http_fetcher=fetcher,
            cache=HttpCache(),
        )
        res = out["results"][0]
        assert res["html"] is None
        assert res["markdown"]


class TestEngineScopeEdges:
    """Cover remaining branches in engine.py"""

    def test_crawl_chain_robots_disallowed(self):
        # _robots_disallowed_result is nested inside crawl_request; exercise it
        # through _skipped_engine_result, the primitive the production path uses
        # (a disallow is a deliberate non-attempt, never a failure).
        from app.constants import status_for_classification
        from app.crawl.engine import _skipped_engine_result

        result = _skipped_engine_result(
            original_url="http://example.com/page",
            normalized_url="http://example.com/page",
            # A disallow precedes any strategy attempt, so the recorded strategy
            # is None, never a label that implies an HTTP fetch happened.
            strategy=None,
            classification="robots_disallowed",
            error="robots_txt_disallowed",
        )
        assert result.classification == "robots_disallowed"
        assert result.status == "skipped"
        assert result.attempted_strategies[0].strategy is None
        # The builder and the taxonomy table must agree for this label.
        assert result.status == status_for_classification("robots_disallowed")

    async def test_crawl_chain_continue_on_skip(self):
        from app.crawl.engine import _execute_crawl_chain
        from app.scheduler import DomainScheduler

        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            class Resp:
                status_code = 200
                headers = {"content-type": "text/html"}
                body = b"<html><title>Access Denied</title>bot challenge</html>"
                final_url = url
                elapsed_ms = 10

            return Resp()

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        policy = {
            "strategies": ["HTTP_DECLARED_CRAWLER", "BROWSER_HEADLESS"],
            "extraction": "full",
            "headers": {},
            "limits": {"timeout_seconds": 5, "max_retries": 0, "max_discovered_links": 10},
            "request_profile": {},
        }
        # HTTP returns access_denied (blocked), browser_worker is None -> browser_crash
        result = await _execute_crawl_chain(
            original_url="http://example.com/page",
            normalized_url="http://example.com/page",
            policy=policy,
            http_fetcher=fetcher,
            cffi_fetcher=fetcher,
            browser_worker=None,
            scheduler=scheduler,
            domain="example.com",
        )
        assert result.classification == "browser_crash"
