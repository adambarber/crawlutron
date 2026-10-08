"""HTTP body handling through a live server: caching, rate-limit cooldown, cffi."""

import pytest

from app.orchestration.engine import crawl_request
from app.transport.cache import HttpCache
from tests._extraction_helpers import default_http_fetcher
from tests._fetcher_factories import run_http_strategy
from tests._policy_factories import _policy, canonical


class TestHttpBodyHandling:
    async def test_cache_last_modified_store(self, base_url):
        cache = HttpCache()
        out = await run_http_strategy(
            url=base_url + "/with_lastmod",
            policy=_policy(),
            http_fetcher=default_http_fetcher,
            cache=cache,
        )
        assert out.status == "success"
        etag, last_modified = cache.get(canonical(base_url + "/with_lastmod"))
        assert (last_modified) is not None

    async def test_rate_limit_cooldown(self, base_url):
        cache = HttpCache()
        out = await run_http_strategy(
            url=base_url + "/ratelimit",
            policy=_policy(),
            http_fetcher=default_http_fetcher,
            cache=cache,
        )
        assert out.http_status == 429
        assert "cooldown" in out.diagnostics
        assert out.diagnostics["cooldown"]["retry_after_seconds"] == 30

    @pytest.mark.real_cffi
    async def test_cffi_fetch_via_crawl_request(self, base_url):
        result = await crawl_request(
            {"url": base_url + "/", "policy": {"strategies": ["HTTP_CFFI_IMPERSONATE"]}},
            cache=HttpCache(),
        )
        assert result["results"][0].status == "success"
