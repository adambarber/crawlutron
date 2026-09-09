"""TestHttpServerGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403


class TestHttpServerGaps:

    def _policy(self, extra=None):
        policy = {"request_profile": {}, "headers": {}, "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50}}
        if extra:
            policy.update(extra)
        return policy

    async def test_cache_last_modified_store(self, base_url):
        from app.crawl.strategies.http import _http_strategy_result
        cache = HttpCache()
        out = await _http_strategy_result(
            original_url=base_url + "/with_lastmod",
            normalized_url=base_url + "/with_lastmod",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=_policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        )
        assert out.status == "success"
        etag, last_modified = cache.get(base_url + "/with_lastmod")
        assert (last_modified) is not None

    async def test_rate_limit_cooldown(self, base_url):
        from app.crawl.strategies.http import _http_strategy_result
        cache = HttpCache()
        out = await _http_strategy_result(
            original_url=base_url + "/ratelimit",
            normalized_url=base_url + "/ratelimit",
            strategy=HTTP_DECLARED_CRAWLER,
            policy=_policy(),
            http_fetcher=_default_http_fetcher,
            cache=cache,
        )
        assert out.http_status == 429
        assert "cooldown" in out.diagnostics
        assert out.diagnostics["cooldown"]["retry_after_seconds"] == 30

    async def test_cffi_fetch_via_crawl_request(self, base_url):
        result = await crawl_request(
            {"url": base_url + "/", "policy": {"strategies": ["HTTP_CFFI_IMPERSONATE"]}},
            cache=HttpCache(),
        )
        assert result["results"][0]["status"] == "success"


