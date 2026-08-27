"""TestEngineGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestEngineGaps:
    # Uses the module-level _fake_browser worker factory.

    def test_result_dict(self):
        d = to_dict(CrawlResult(url="http://x/", status="success", classification="rendered",
                                    final_url="http://x/", html=None, text="t", attempts=1,
                                    elapsed_ms=1, domain="x", metadata={}))
        assert d["url"] == "http://x/"

    def test_payload_policy_invalid_strategy(self):
        with pytest.raises(ValueError):
            _payload_policy({"policy": {"strategies": ["BOGUS"]}}, None)

    async def test_browser_strategy_document_extraction(self):
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_DOCUMENT}
        result = await crawl_request(
            {"url": "http://127.0.0.1:1/doc", "policy": policy},
            browser_worker=_fake_browser(html="<html><body>doc table</body></html>"),
            cache=HttpCache(),
        )
        assert result["results"][0]["status"] == "success"
        assert "doc table" in result["results"][0]["markdown"] or ""

    async def test_browser_strategy_trafilatura_extraction(self):
        policy = {"strategies": [BROWSER_HEADLESS], "extraction": EXTRACTION_TRAFILATURA}
        result = await crawl_request(
            {"url": "http://127.0.0.1:1/doc2", "policy": policy},
            browser_worker=_fake_browser(html="<html><body>trafilatura body</body></html>"),
            cache=HttpCache(),
        )
        assert "trafilatura body" in result["results"][0]["markdown"] or ""

    async def test_browser_strategy_no_content(self):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = await crawl_request(
            {"url": "http://127.0.0.1:1/doc3", "policy": policy},
            browser_worker=_fake_browser(html=None, text=None),
            cache=HttpCache(),
        )
        assert (result["results"][0]["markdown"]) is None

    async def test_browser_strategy_surfaces_xhr_and_screenshot(self):
        policy = {"strategies": [BROWSER_HEADLESS]}
        result = await crawl_request(
            {"url": "http://127.0.0.1:1/doc4", "policy": policy},
            browser_worker=_fake_browser(
                html="<html><body>x</body></html>",
                metadata={"intercepted_xhr": [{"url": "u", "payload": {}}], "screenshot": "BASE64IMG"},
            ),
            cache=HttpCache(),
        )
        diag = result["results"][0]["diagnostics"]
        assert "intercepted_xhr" in diag
        assert "screenshot" in diag


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

