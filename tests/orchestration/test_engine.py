"""Crawl engine: strategy dispatch, chain edges and scope resolution."""

import asyncio
import urllib.error
import warnings

import pytest

from crawlutron.admission.policy import PolicyTier, read_policy
from crawlutron.models.crawl_policy import CrawlPolicy
from crawlutron.models.crawl_policy_limits import CrawlPolicyLimits
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import CrawlResult
from crawlutron.models.json_value import JsonValue
from crawlutron.models.result_factory import terminal_attempt
from crawlutron.models.serialization import to_dict
from crawlutron.models.taxonomy import Classification
from crawlutron.transport.browser import BrowserRenderer
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.extraction import EXTRACTION_DOCUMENT, EXTRACTION_TRAFILATURA
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from crawlutron.vocabulary.taxonomy import CrawlClassification
from tests._assertions import browser_integration_test
from tests._browser_doubles import _fake_browser
from tests._fetch_doubles import FetcherDouble
from tests._fetcher_factories import (
    cache_with_etag,
    crawled_result,
    run_chain,
)
from tests._policy_factories import (
    canonical,
    http_strategy_policy,
    payload_policy,
)


def http_policy(*strategies: str, extraction: str = "full", **extra) -> CrawlPolicy:
    """The HTTP strategy's policy with a different strategy list or one more key.

    Derived from the shared policy factory rather than re-spelled, so the
    limits and empty header mappings are that factory's to state.
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
        first_markdown = response["results"][0].markdown or ""
        assert "Example Org" in first_markdown

    async def test_crawl_request_conditional_cache_304(self, base_url):
        url = f"{base_url}/with_etag"
        result = await crawled_result(url, max_discovered_links=10, cache=cache_with_etag(url))
        assert result.classification == "not_modified"
        assert result.http_status == 304

    async def test_crawl_request_browser_strategy_without_worker_is_browser_crash(self, base_url):
        result = await crawled_result(f"{base_url}/", strategies=[BROWSER_HEADLESS], max_discovered_links=10)
        assert result.status == "failed"
        assert result.classification == "browser_crash"

    @browser_integration_test
    async def test_crawl_request_http_failure_falls_back_to_browser(self, base_url):

        # The intent here is page-fetch failure -> browser fallback.
        _robots = (200, "User-agent: *\nAllow: /\n", "text/plain")
        failing_fetcher = FetcherDouble(
            lambda url, _sent_headers, *_: (
                _robots if "/robots.txt" in url.raw else urllib.error.URLError("connection refused")
            )
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
        assert result.status == "success"
        assert result.classification == "rendered"
        assert "Example Org" in (result.markdown or "")


def test_seed_freshness_uses_real_default_strategies():
    """Without an explicit strategies list, freshness must probe the
    real default chain -- not the placeholder ["HTTP"], whose cache keys
    can never match anything actually stored."""
    from crawlutron.models.crawl_extraction_method import CrawlExtractionMethod
    from crawlutron.orchestration.engine import parse_lastmod, seed_is_fresh
    from crawlutron.transport.content_cache_key import build_content_cache_key
    from crawlutron.transport.request_profile import headers_for_strategy
    from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER

    url = canonical("http://seed-fresh.test/page")
    # Normalized shape but no strategies key: the default must apply.
    policy: dict[str, JsonValue] = {"headers": {}, "request_profile": {}}
    expected_key = build_content_cache_key(
        url,
        HTTP_DECLARED_CRAWLER,
        headers_for_strategy(HTTP_DECLARED_CRAWLER, read_policy(policy), url),
        CrawlExtractionMethod.FULL,
        None,
        None,
    )
    cached_at = parse_lastmod("2024-06-01")

    class _StubCache:
        def cached_at_of(self, key):
            return cached_at if key == expected_key else None

    assert seed_is_fresh(url, "2024-01-01", _StubCache(), read_policy(policy)) is True


class TestCrawlRequestWebhook:
    async def test_engine_crawl_request_with_webhook(self):
        import asyncio

        from crawlutron.config.settings import CrawlerSettings
        from crawlutron.models.host_policy import HostPolicy
        from crawlutron.models.webhook_target import WebhookTarget
        from crawlutron.orchestration.engine import crawl_request

        async def browser_worker(job):
            return None

        result = await crawl_request(
            {"url": "http://127.0.0.1:1/", "policy": http_policy(extraction="full")},
            settings=CrawlerSettings.from_mapping(),
            browser_worker=browser_worker,
            shutdown_event=asyncio.Event(),
            webhook_url=WebhookTarget.admit("https://webhook.invalid", host_policy=HostPolicy.unrestricted()),
        )
        assert "webhook_url" in result


class TestEngineChainEdges:
    # Uses the module-level _fake_browser worker factory.

    def test_result_dict(self):
        attempt = terminal_attempt(
            strategy=None,
            status="success",
            classification=Classification.rendered,
            elapsed_ms=1,
            error=None,
        )
        d = to_dict(
            CrawlResult(
                url=canonical("http://x/"),
                attempt=attempt,
                final_url=canonical("http://x/"),
                html=None,
                text="t",
                attempts=1,
                domain="x",
                metadata={},
            )
        )
        assert d["url"] == "http://x/"

    def test_payload_policy_rejects_wire_dict(self):
        # The typed funnel takes a validated CrawlRequest only; a model carrying
        # a strategy the vocabulary refuses must not slip past the funnel either.
        from crawlutron.models.crawl_policy import CrawlPolicy

        forged = CrawlPolicy.model_construct()
        # __dict__-level mutation: the funnel, not pydantic, is the subject.
        object.__setattr__(forged, "strategies", ["BOGUS"])
        with pytest.raises(ValueError, match="invalid_policy_strategy"):
            with warnings.catch_warnings():
                # model_construct bypasses field validation by design; the funnel
                # (not pydantic) is the subject, so pydantic's serializer warning is noise.
                warnings.simplefilter("ignore")
                payload_policy(
                    CrawlRequest.model_construct(url="http://x/", policy=forged), PolicyTier.model_defaults()
                )

    async def test_browser_strategy_document_extraction(self, base_url):
        # Live origin: 's robots gate fail-closes connection-refused origins (127.0.0.1:1), so the subject
        # here needs a server that 404s robots.
        result = await crawled_result(
            f"{base_url}/doc",
            policy={"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT},
            browser_worker=_fake_browser(html="<html><body>doc table</body></html>"),
        )
        assert result.status == "success"
        assert "doc table" in (result.markdown or "")

    async def test_browser_strategy_trafilatura_extraction(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc2",
            policy={"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA},
            browser_worker=_fake_browser(html="<html><body>trafilatura body</body></html>"),
        )
        assert "trafilatura body" in (result.markdown or "")

    async def test_browser_strategy_no_content(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc3",
            policy={"strategies": [BROWSER_HEADLESS]},
            browser_worker=_fake_browser(html=None, text=None),
        )
        assert (result.markdown) is None

    async def test_browser_strategy_surfaces_xhr_and_screenshot(self, base_url):
        result = await crawled_result(
            f"{base_url}/doc4",
            policy={"strategies": [BROWSER_HEADLESS]},
            browser_worker=_fake_browser(
                html="<html><body>x</body></html>",
                metadata={
                    "intercepted_xhr": [{"url": "u", "payload": {}}],
                    "screenshot": [{"selector": "s", "screenshot": "BASE64IMG"}],
                },
            ),
        )
        diag = result.diagnostics
        assert "intercepted_xhr" in diag
        assert "screenshot" in diag

    async def test_per_url_budget_fails_slow_chain(self):
        """A URL that blows its total_url_budget_seconds
        must be failed as network_error instead of holding a slot."""

        # True tarpit: never returns until the caller's deadline cancels us.
        async def _never(url, *_):
            await asyncio.Future()

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
            http_fetcher=FetcherDouble(_never),
        )
        assert result.status == "failed"
        # The budget's classification is precise (url_budget_exceeded), not the generic network_error tail the
        # old hand-built result hard-coded.
        assert result.classification == "url_budget_exceeded"
        assert result.error == "url_budget_exceeded"

    async def test_rate_limited_sets_domain_cooldown(self):
        fetcher = FetcherDouble(
            lambda url, *_: {
                "status": 429,
                "headers": {"retry-after": "30", "content-type": "text/plain"},
                "body": b"rate limited",
            }
        )

        now = [1000.0]
        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, clock=lambda: now[0]
        )
        result = await run_chain(
            "http://example.com/", fetcher, policy=http_policy(extraction="dom"), scheduler=scheduler
        )
        assert result.classification == "rate_limited"
        cooldowns = scheduler.domain_cooldowns
        assert "example.com" in cooldowns
        # The park is the Retry-After duration on the ONE clock, not a wall
        # stamp (#580): still standing at 30-5, gone past 30.
        now[0] += 25.0
        scheduler.purge_expired()
        assert "example.com" in cooldowns
        now[0] += 10.0
        scheduler.purge_expired()
        assert "example.com" not in scheduler.domain_cooldowns

    async def test_robots_crawl_delay_sets_domain_min_delay(self, monkeypatch):
        """A host's robots Crawl-delay is honored as a
        per-domain minimum delay during the crawl."""
        calls = []
        orig = DomainScheduler.set_domain_min_delay

        def spy(self, domain, seconds):
            calls.append((domain, seconds))
            return orig(self, domain, seconds)

        monkeypatch.setattr(DomainScheduler, "set_domain_min_delay", spy)

        _robots = (200, "User-agent: *\nCrawl-delay: 2\n", "text/html")
        _home = (200, b"<html><body>ok</body></html>", "text/html")
        await crawled_result(
            "http://example.com/",
            policy=http_policy(extraction="dom"),
            http_fetcher=FetcherDouble(
                lambda url, _sent_headers, *_: _robots if "/robots.txt" in url.raw else _home
            ),
        )
        assert any(d == "example.com" and abs(s - 2.0) < 1e-6 for d, s in calls), calls

    def test_effective_max_content_length_scales(self):
        """Per-page cap shrinks as concurrency rises, floor
        respected, explicit policy value wins."""
        from crawlutron.transport.content_cache_key import effective_max_content_length
        from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

        # Explicit value always wins.
        assert effective_max_content_length(CrawlPolicyLimits(max_content_length_bytes=123)) == 123
        # Default concurrency (4) -> unchanged 10 MiB default.
        assert effective_max_content_length(CrawlPolicyLimits()) == DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # High concurrency shrinks the cap.
        small = effective_max_content_length(CrawlPolicyLimits(), global_concurrency=400)
        assert small < DEFAULT_MAX_CONTENT_LENGTH_BYTES
        # Floor is respected even at absurd concurrency.
        assert small >= 256 * 1024
        assert effective_max_content_length(CrawlPolicyLimits(), global_concurrency=100000) == 256 * 1024

    async def test_include_html_false_drops_html(self):
        """When include_html is False the raw HTML is not retained in the result."""
        result = await crawled_result(
            "http://example.com/",
            policy=http_policy(extraction="dom", browser_profile={"include_html": False}),
            http_fetcher=FetcherDouble(
                lambda url, *_: {
                    "status": 200,
                    "ctype": "text/html",
                    "body": b"<html><body>some page content</body></html>",
                }
            ),
        )
        assert result.html is None
        assert result.markdown


class TestEngineScopeEdges:
    """Cover remaining branches in engine.py"""

    def test_crawl_chain_robots_disallowed(self):
        from crawlutron.transport.results import (
            TerminalResult,
            skipped_engine_result,
        )
        from crawlutron.vocabulary.taxonomy import status_for_classification

        result = skipped_engine_result(
            TerminalResult(
                original_url="http://example.com/page",
                normalized_url=canonical("http://example.com/page"),
                # A disallow precedes any strategy attempt, so the recorded strategy is None, never a label
                # that implies an HTTP fetch happened.
                strategy=None,
                classification=CrawlClassification.robots_disallowed,
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
            FetcherDouble(
                lambda url, *_: {
                    "status": 200,
                    "ctype": "text/html",
                    "body": b"<html><title>Access Denied</title>bot challenge</html>",
                }
            ),
            policy=http_policy(HTTP_DECLARED_CRAWLER, BROWSER_HEADLESS),
        )
        assert result.classification == "browser_crash"
