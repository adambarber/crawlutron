"""TestEngineGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *


class TestEngineGaps:
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
        assert result["classification"] == "network_error"
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
            from app.crawl.strategies.http import HttpFetchResult

            if url.endswith("/robots.txt"):
                body = b"User-agent: *\nCrawl-delay: 2\n"
            else:
                body = b"<html><body>ok</body></html>"
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                body=body,
                elapsed_ms=1,
            )

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
        from app.crawl.strategies.http import HttpFetchResult

        def fetcher(url, headers, timeout_seconds, max_content_length=50 * 1024 * 1024):
            body = b"<html><body>some page content</body></html>"
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=200,
                headers={"content-type": "text/html"},
                body=body,
                elapsed_ms=1,
            )

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
