"""The revalidation path must fetch exactly once.

A cached entry with a validator is revalidated with a conditional GET. When the
origin answers 200 (content changed) that response must be used and stored; the
code previously fell through to a second, unconditional fetch because neither
``content_hit`` nor ``revalidated_304`` was set on the 200 branch -- two real
round-trips and doubled origin load for one crawl.
"""

from _testkit import *  # noqa: F403 (test wildcard seam)


class _CountingFetcher:
    """A fetcher that counts calls and always returns a fresh 200.

    Stands in for the network boundary only; ``_http_strategy_result`` itself
    runs for real, so this exercises the production cache+revalidation control
    flow rather than a substituted shape.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.header_sets: list[dict] = []

    async def __call__(self, url, headers, timeout_seconds, max_content_length):
        self.calls += 1
        self.header_sets.append(dict(headers))
        return make_fetch(
            status=200,
            url=url,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8", "etag": "v1"},
            body=b"<html><body>content</body></html>",
        )


class TestRevalidationFetchesOnce:
    def _policy(self):
        return {
            "request_profile": {},
            "headers": {},
            "limits": {"timeout_seconds": 10, "max_retries": 0, "max_discovered_links": 50},
        }

    async def test_validator_hit_with_200_origin_does_not_refetch(self, tmp_path):
        from app.crawl.strategies.http import _http_strategy_result

        fetcher = _CountingFetcher()
        url = "http://example.com/etagged"
        cache = HttpCache()
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)

        # First crawl: no cache entry yet, one fetch, stores it with its etag.
        first = await _http_strategy_result(
            original_url=url,
            normalized_url=url,
            strategy=HTTP_DECLARED_CRAWLER,
            policy=self._policy(),
            http_fetcher=fetcher,
            cache=cache,
            content_cache=content_cache,
        )
        assert first.status == "success"
        assert fetcher.calls == 1, fetcher.calls

        # Second crawl: cache hit with a validator. The origin returns 200
        # (content "changed"), so the revalidation response is used directly.
        # The bug re-fetched unconditionally here, giving 3 fetches total.
        second = await _http_strategy_result(
            original_url=url,
            normalized_url=url,
            strategy=HTTP_DECLARED_CRAWLER,
            policy=self._policy(),
            http_fetcher=fetcher,
            cache=cache,
            content_cache=content_cache,
        )
        assert second.status == "success"
        assert fetcher.calls == 2, (
            f"expected one fetch for the revalidation, got {fetcher.calls - 1}"
        )
        # The second fetch must be the conditional one, not an unconditional repeat.
        assert fetcher.header_sets[1].get("If-None-Match") == "v1"
