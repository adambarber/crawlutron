"""Crawl engine: strategy dispatch, chain edges and scope resolution."""

import time
import urllib.error

import pytest

from app.api_models import CrawlPolicy, CrawlPolicyLimits
from app.constants import BROWSER_HEADLESS, EXTRACTION_DOCUMENT, EXTRACTION_TRAFILATURA, HTTP_DECLARED_CRAWLER
from app.crawl.engine import _payload_policy
from app.crawl.policy import read_policy
from app.crawl.strategies.browser import BrowserRenderer
from app.models import CrawlResult, to_dict
from app.scheduler import DomainScheduler
from tests._assertions import browser_available
from tests._browser_doubles import _fake_browser
from tests._fetcher_factories import (
    cache_with_etag,
    crawled_result,
    hanging_fetcher,
    routed_fetcher,
    run_chain,
    scripted_fetcher,
)
from tests._policy_factories import http_strategy_policy


def http_policy(*strategies: str, extraction: str = "full", **extra) -> CrawlPolicy:
    """The HTTP strategy's policy with a different strategy list or one more key.

    Derived from the shared policy factory rather than re-spelled: the limits and
    the empty header mappings are that factory's to state, and a test says only
    the part it is changing.
    """
    raw = http_strategy_policy(extraction=extraction).model_dump()
    if strategies:
        raw["strategies"] = list(strategies)
    raw.update(extra)
    return read_policy(raw)


class TestEngineCrawlRequest:
    async def test_crawl_request_http_success(self, base_url):
        from tests._fetcher_factories import crawl_homepage_async

        response = await crawl_homepage_async(base_url)
        assert "Example Org" in response["results"][0]["markdown"]

    async def test_crawl_request_conditional_cache_304(self, base_url):
        url = f"{base_url}/with_etag"
        result = await crawled_result(url, max_discovered_links=10, cache=cache_with_etag(url))
        assert result["classification"] == "not_modified"
        assert result["http_status"] == 304

    async def test_crawl_request_browser_strategy_without_worker_is_browser_crash(self, base_url):
        result = await crawled_result(f"{base_url}/", strategies=[BROWSER_HEADLESS], max_discovered_links=10)
        assert result["status"] == "failed"
        assert result["classification"] == "browser_crash"

    @pytest.mark.browser
    @pytest.mark.skipif(not browser_available(), reason="no usable browser binary (camoufox/playwright)")
    async def test_crawl_request_http_failure_falls_back_to_browser(self, base_url):

        # Fail page fetches but serve robots.txt: robots is per-origin
        # (M7 fail-closed), so a fake that also breaks robots would
        # skip the crawl before the browser fallback is ever reached.
        # The intent here is page-fetch failure -> browser fallback.
        failing_fetcher = routed_fetcher(
            {"/robots.txt": (200, "User-agent: *\nAllow: /\n", "text/plain")},
            default=urllib.error.URLError("connection refused"),
        )

        renderer = BrowserRenderer()

        async def browser_worker(job):
            return await renderer.render(
                job.url,
                job.options,
                policy=job.host_policy,
            )

        result = await crawled_result(
            f"{base_url}/",
            strategies=[HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS],
            timeout_seconds=20,
            max_discovered_links=50,
            http_fetcher=failing_fetcher,
            browser_worker=browser_worker,
        )
        assert result["status"] == "success"
        assert result["classification"] == "rendered"
        assert "Example Org" in result["markdown"]


def test_seed_freshness_uses_real_default_strategies():
    """L5b: without an explicit strategies list, freshness must probe the
    real default chain -- not the placeholder ["HTTP"], whose cache keys
    can never match anything actually stored."""
    from app.api_models import CrawlExtractionMethod
    from app.constants import HTTP_DECLARED_CRAWLER
    from app.crawl.engine import _parse_lastmod, _seed_is_fresh
    from app.crawl.strategies.content_cache_key import _content_cache_key
    from app.crawl.strategies.request_profile import _headers_for_strategy

    url = "http://seed-fresh.test/page"
    # Normalized shape but no strategies key: the default must apply.
    policy = {"headers": {}, "request_profile": {}}
    expected_key = _content_cache_key(
        url,
        HTTP_DECLARED_CRAWLER,
        _headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy(policy), url),
        CrawlExtractionMethod.FULL,
        None,
        None,
    )
    cached_at = _parse_lastmod("2024-06-01")

    class _StubCache:
        def cached_at_of(self, key):
            return cached_at if key == expected_key else None

    assert _seed_is_fresh(url, "2024-01-01", _StubCache(), read_policy(policy)) is True


class TestCrawlRequestWebhook:
    async def test_engine_crawl_request_with_webhook(self):
        import asyncio

        from app.crawl.engine import crawl_request
        from app.settings import CrawlerSettings

        async def browser_worker(job):
            return None

        result = await crawl_request(
            {"url": "http://127.0.0.1:1/", "policy": http_policy(extraction="full")},
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
        result = await crawled_result(
            f"{base_url}/doc",
            policy={"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT},
            browser_worker=_fake_browser(html="<html><body>doc table</body></html>"),
        )
        assert result["status"] == "success"
        assert "doc table" in result["markdown"] or ""

    async def test_browser_strategy_trafilatura_extraction(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc2",
            policy={"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA},
            browser_worker=_fake_browser(html="<html><body>trafilatura body</body></html>"),
        )
        assert "trafilatura body" in result["markdown"] or ""

    async def test_browser_strategy_no_content(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc3",
            policy={"strategies": [BROWSER_HEADLESS]},
            browser_worker=_fake_browser(html=None, text=None),
        )
        assert (result["markdown"]) is None

    async def test_browser_strategy_surfaces_xhr_and_screenshot(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc4",
            policy={"strategies": [BROWSER_HEADLESS]},
            browser_worker=_fake_browser(
                html="<html><body>x</body></html>",
                metadata={"intercepted_xhr": [{"url": "u", "payload": {}}], "screenshot": "BASE64IMG"},
            ),
        )
        diag = result["diagnostics"]
        assert "intercepted_xhr" in diag
        assert "screenshot" in diag

    async def test_per_url_budget_fails_slow_chain(self):
        """Adversarial perf #2.2: a URL that blows its total_url_budget_seconds
        must be failed as network_error instead of holding a slot."""

        # True tarpit: never returns until the caller's deadline cancels us.
        # (A time-based sleep is monkeypatched to return instantly in this
        # suite's fast mode, so model the poisoned-URL hang with the harness's
        # ever-pending future instead -- which is what hanging_fetcher is.)
        result = await crawled_result(
            "http://example.com/slow",
            policy={
                "strategies": [HTTP_DECLARED_CRAWLER],
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
            },
            http_fetcher=hanging_fetcher(),
        )
        assert result["status"] == "failed"
        # The budget's classification is precise (url_budget_exceeded), not the
        # generic network_error tail the old hand-built result hard-coded.
        assert result["classification"] == "url_budget_exceeded"
        assert result["error"] == "url_budget_exceeded"

    async def test_rate_limited_sets_domain_cooldown(self):
        fetcher = scripted_fetcher(
            status=429,
            headers={"retry-after": "30", "content-type": "text/plain"},
            body=b"rate limited",
        )

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        result = await run_chain(
            "http://example.com/", fetcher, policy=http_policy(extraction="dom"), scheduler=scheduler
        )
        assert result.classification == "rate_limited"
        assert "example.com" in scheduler._domain_cooldowns
        assert abs(scheduler._domain_cooldowns["example.com"] - (time.time() + 30)) <= 5

    async def test_robots_crawl_delay_sets_domain_min_delay(self, monkeypatch):
        """Adversarial perf #2.6: a host's robots Crawl-delay is honored as a
        per-domain minimum delay during the crawl."""
        calls = []
        orig = DomainScheduler.set_domain_min_delay

        def spy(self, domain, seconds):
            calls.append((domain, seconds))
            return orig(self, domain, seconds)

        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", spy)

        await crawled_result(
            "http://example.com/",
            policy=http_policy(extraction="dom"),
            http_fetcher=routed_fetcher(
                {"/robots.txt": (200, "User-agent: *\nCrawl-delay: 2\n", "text/html")},
                default=(200, b"<html><body>ok</body></html>", "text/html"),
            ),
        )
        assert any(d == "example.com" and abs(s - 2.0) < 1e-6 for d, s in calls), calls

    def test_effective_max_content_length_scales(self):
        """Adversarial perf #2.1: per-page cap shrinks as concurrency rises, floor
        respected, explicit policy value wins."""
        from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES
        from app.crawl.strategies.content_cache_key import _effective_max_content_length

        # Explicit value always wins.
        assert _effective_max_content_length(CrawlPolicyLimits(max_content_length_bytes=123)) == 123
        # Default concurrency (4) -> unchanged 10 MiB default.
        assert _effective_max_content_length(CrawlPolicyLimits()) == DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # High concurrency shrinks the cap.
        small = _effective_max_content_length(CrawlPolicyLimits(), global_concurrency=400)
        assert small < DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # Floor is respected even at absurd concurrency.
        assert small >= 256 * 1024
        assert _effective_max_content_length(CrawlPolicyLimits(), global_concurrency=100000) == 256 * 1024

    async def test_include_html_false_drops_html(self):
        """When include_html is False the raw HTML is not retained in the result."""
        result = await crawled_result(
            "http://example.com/",
            policy=http_policy(extraction="dom", browser_profile={"include_html": False}),
            http_fetcher=scripted_fetcher(
                status=200, ctype="text/html", body=b"<html><body>some page content</body></html>"
            ),
        )
        assert result["html"] is None
        assert result["markdown"]


class TestEngineScopeEdges:
    """Cover remaining branches in engine.py"""

    def test_crawl_chain_robots_disallowed(self):
        from app.constants import status_for_classification
        from app.crawl.result_helpers import TerminalResult, _skipped_engine_result

        result = _skipped_engine_result(
            TerminalResult(
                original_url="http://example.com/page",
                normalized_url="http://example.com/page",
                # A disallow precedes any strategy attempt, so the recorded strategy
                # is None, never a label that implies an HTTP fetch happened.
                strategy=None,
                classification="robots_disallowed",
                error="robots_txt_disallowed",
            )
        )
        assert result.classification == "robots_disallowed"
        assert result.status == "skipped"
        assert result.attempted_strategies[0].strategy is None
        # The builder and the taxonomy table must agree for this label.
        assert result.status == status_for_classification("robots_disallowed")

    async def test_crawl_chain_continue_on_skip(self):
        # HTTP returns access_denied (blocked), browser_worker is None -> browser_crash
        result = await run_chain(
            "http://example.com/page",
            scripted_fetcher(
                status=200,
                ctype="text/html",
                body=b"<html><title>Access Denied</title>bot challenge</html>",
            ),
            policy=http_policy(HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS),
        )
        assert result.classification == "browser_crash"
