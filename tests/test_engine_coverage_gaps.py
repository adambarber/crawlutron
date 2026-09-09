"""TestEngineCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403


class TestEngineCoverageGaps:
    """Cover remaining branches in engine.py"""

    def test_crawl_chain_robots_disallowed(self):
        # _robots_disallowed_result is nested inside crawl_request; test via _failed_engine_result
        from app.crawl.engine import _failed_engine_result
        result = _failed_engine_result(
            original_url="http://example.com/page",
            normalized_url="http://example.com/page",
            strategy="HTTP_DECLARED_CRAWLER",
            classification="robots_disallowed",
            error="robots_txt_disallowed",
        )
        assert result.classification == "robots_disallowed"

    async def test_crawl_chain_continue_on_skip(self):
        from app.crawl.engine import _execute_crawl_chain
        from app.scheduler import DomainScheduler

        def fetcher(url, headers, timeout, max_content_length=50*1024*1024):
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


