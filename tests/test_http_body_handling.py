"""HTTP body handling through a live server: caching, rate-limit cooldown, cffi."""

import pytest
from _extraction_helpers import _default_http_fetcher
from _policy_factories import _policy

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.validation import HostPolicy


class TestHttpBodyHandling:
    def _policy(self, extra=None):
        policy = {
            "request_profile": {},
            "headers": {},
            "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
        }
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
            host_policy=HostPolicy.unrestricted(),
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
            host_policy=HostPolicy.unrestricted(),
        )
        assert out.http_status == 429
        assert "cooldown" in out.diagnostics
        assert out.diagnostics["cooldown"]["retry_after_seconds"] == 30

    @pytest.mark.real_cffi
    async def test_cffi_fetch_via_crawl_request(self, base_url, real_sleeps):
        result = await crawl_request(
            {"url": base_url + "/", "policy": {"strategies": ["HTTP_CFFI_IMPERSONATE"]}},
            cache=HttpCache(),
        )
        assert result["results"][0]["status"] == "success"
