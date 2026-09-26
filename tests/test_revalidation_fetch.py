"""The revalidation path must fetch exactly once.

A cached entry with a validator is revalidated with a conditional GET. When the
origin answers 200 (content changed) that response must be used and stored; the
code previously fell through to a second, unconditional fetch because neither
``content_hit`` nor ``revalidated_304`` was set on the 200 branch -- two real
round-trips and doubled origin load for one crawl.
"""

from _fetcher_factories import make_fetch, run_http_strategy

from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache


async def _run_revalidation(url, policy, fetcher, cache, content_cache):
    return await run_http_strategy(
        url=url,
        policy=policy,
        http_fetcher=fetcher,
        cache=cache,
        content_cache=content_cache,
    )


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
        fetcher = _CountingFetcher()
        url = "http://example.com/etagged"
        cache = HttpCache()
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)

        # First crawl: no cache entry yet, one fetch, stores it with its etag.
        first = await _run_revalidation(url, self._policy(), fetcher, cache, content_cache)
        assert first.status == "success"
        assert fetcher.calls == 1, fetcher.calls

        # Second crawl: cache hit with a validator. The origin returns 200
        # (content "changed"), so the revalidation response is used directly.
        # The bug re-fetched unconditionally here, giving 3 fetches total.
        second = await _run_revalidation(url, self._policy(), fetcher, cache, content_cache)
        assert second.status == "success"
        assert fetcher.calls == 2, f"expected one fetch for the revalidation, got {fetcher.calls - 1}"
        # The second fetch must be the conditional one, not an unconditional repeat.
        assert fetcher.header_sets[1].get("If-None-Match") == "v1"

    async def test_origin_fresh_200_is_stored_and_later_304s_serve_it(self, tmp_path):
        """The revalidated 200 must LAND IN THE CACHE, not just be used.

        The double-fetch fix briefly reused ``content_hit`` ("served from the
        content cache -> do not write back") to also mean "a response is in
        hand", which silently skipped the store: later crawls revalidated
        against -- and served -- the stale pre-revalidation body forever.
        Fetch-count assertions cannot see this (conditional GETs are the
        design); only WHICH BODY a later 304 serves discriminates. Response-
        in-hand is dataflow (response is not None); served-from-cache is the
        flag's only meaning.
        """

        class _ScriptedFetcher(_CountingFetcher):
            def __init__(self) -> None:
                super().__init__()
                self.body = b"<html><body>FRESH-BODY</body></html>"

            async def __call__(self, url, headers, timeout_seconds, max_content_length):
                self.calls += 1
                self.header_sets.append(dict(headers))
                if self.calls >= 3:  # origin reports unchanged from crawl 3 on
                    return make_fetch(status=304, url=url, final_url=url, headers={"etag": "v1"}, body=b"")
                return make_fetch(
                    status=200,
                    url=url,
                    final_url=url,
                    headers={"content-type": "text/html; charset=utf-8", "etag": "v1"},
                    body=self.body,
                )

        fetcher = _ScriptedFetcher()
        url = "http://example.com/etagged-then-stored"
        cache = HttpCache()
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)

        async def _crawl():
            result = await _run_revalidation(url, self._policy(), fetcher, cache, content_cache)
            assert result.status == "success"
            return result

        fetcher.body = b"<html><body>ORIGINAL-BODY</body></html>"
        await _crawl()  # crawl 1: one fetch, stores ORIGINAL
        fetcher.calls = 0
        fetcher.body = b"<html><body>FRESH-BODY</body></html>"
        second = await _crawl()  # crawl 2: conditional 200 -> FRESH must be stored
        assert "FRESH-BODY" in second.text or "FRESH-BODY" in second.markdown
        assert fetcher.calls == 1

        third = await _crawl()  # crawl 3: conditional 304 -> serves the STORED body
        body = third.text or third.markdown or ""
        assert "FRESH-BODY" in body, (
            "a later 304 served the stale pre-revalidation body: the revalidated 200 was never stored"
        )
        assert fetcher.calls == 2  # one conditional GET per crawl, as designed
