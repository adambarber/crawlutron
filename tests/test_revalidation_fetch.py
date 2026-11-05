"""The revalidation path must fetch exactly once: a cached entry revalidates with a
conditional GET, and a 200 answer must be used and stored -- the code once fell
through to a second unconditional fetch (double load for one crawl).
"""

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from tests._fetch_doubles import make_fetch
from tests._fetcher_factories import run_http_strategy


class _CountingFetcher:
    """A fetcher that counts calls and always returns a fresh 200, standing in for the
    network only -- the strategy runs for real. The 200 is its own method so a scripted
    subclass overrides WHICH response, not the shared bookkeeping that can drift.
    """

    etag = "v1"
    body = b"<html><body>content</body></html>"

    def __init__(self) -> None:
        self.calls = 0
        self.header_sets: list[dict[str, str]] = []

    async def __call__(
        self, url: NormalizedUrl, headers: dict[str, str], timeout_seconds: int, max_content_length: int = 0
    ) -> HttpFetchResult:
        self.calls += 1
        self.header_sets.append(dict(headers))
        return self._response(url)

    def _response(self, url):
        return make_fetch(
            status=200,
            url=url,
            final_url=url,
            headers={"content-type": "text/html; charset=utf-8", "etag": self.etag},
            body=self.body,
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
        first = await run_http_strategy(
            url=url, policy=self._policy(), http_fetcher=fetcher, cache=cache, content_cache=content_cache
        )
        assert first.status == "success"
        assert fetcher.calls == 1, fetcher.calls

        # Second crawl: cache hit with a validator.
        second = await run_http_strategy(
            url=url, policy=self._policy(), http_fetcher=fetcher, cache=cache, content_cache=content_cache
        )
        assert second.status == "success"
        assert fetcher.calls == 2, f"expected one fetch for the revalidation, got {fetcher.calls - 1}"
        # The second fetch must be the conditional one, not an unconditional repeat.
        assert fetcher.header_sets[1].get("If-None-Match") == "v1"

    async def test_origin_fresh_200_is_stored_and_later_304s_serve_it(self, tmp_path):
        """The revalidated 200 must LAND IN THE CACHE, not just be used: the double-fetch
        fix briefly misread ``content_hit`` as "response in hand", skipping the store, so
        later 304s served stale forever -- only which body a 304 serves discriminates.
        """

        class _ScriptedFetcher(_CountingFetcher):
            """A fresh 200 until the third call, then a permanent 304. ``body`` is an instance
            attribute the test rewrites between crawls -- that rewrite is how stale-versus-
            stored is set up, so the response shape is inherited.
            """

            def __init__(self) -> None:
                super().__init__()
                self.body = b"<html><body>FRESH-BODY</body></html>"

            def _response(self, url):
                if self.calls >= 3:  # origin reports unchanged from crawl 3 on
                    return make_fetch(
                        status=304, url=url, final_url=url, headers={"etag": self.etag}, body=b""
                    )
                return super()._response(url)

        fetcher = _ScriptedFetcher()
        url = "http://example.com/etagged-then-stored"
        cache = HttpCache()
        content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)

        async def _crawl():
            result = await run_http_strategy(
                url=url, policy=self._policy(), http_fetcher=fetcher, cache=cache, content_cache=content_cache
            )
            assert result.status == "success"
            return result

        fetcher.body = b"<html><body>ORIGINAL-BODY</body></html>"
        await _crawl()  # crawl 1: one fetch, stores ORIGINAL
        fetcher.calls = 0
        fetcher.body = b"<html><body>FRESH-BODY</body></html>"
        second = await _crawl()  # crawl 2: conditional 200 -> FRESH must be stored
        assert "FRESH-BODY" in second.text or "FRESH-BODY" in second.markdown
        assert fetcher.calls == 1

        third = await _crawl()  # crawl 3: conditional 200 -> serves the freshly-revalidated body
        body = third.text or third.markdown or ""
        assert "FRESH-BODY" in body, (
            "a later 200 served the stale pre-revalidation body: the revalidated 200 was never stored"
        )
        assert fetcher.calls == 2  # one conditional GET per crawl, as designed


async def test_content_cache_revalidation_304_reports_not_modified(tmp_path):
    """A content-cache revalidation that earns a 304 must classify not_modified, not a fresh render.
    WatchSweeper keys on not_modified to suppress a change webhook; a 304 served as 'rendered'
    fires modified forever (the #236 regression)."""
    from tests._fetch_doubles import FetcherDouble

    etag = '"v1"'

    def respond(url, headers):
        if headers.get("If-None-Match") == etag:
            return {"status": 304, "headers": {"etag": etag}, "body": b""}
        return {"status": 200, "headers": {"etag": etag}, "body": b"<html>STOC</html>"}

    fetcher = FetcherDouble(lambda url, sent_headers, *_: respond(url, sent_headers))
    cache = HttpCache()
    content_cache = ContentCache(cache_dir=str(tmp_path / "cc"), default_ttl=3600)

    async def _crawl():
        return await run_http_strategy(
            url="http://example.com/reval",
            policy={},
            http_fetcher=fetcher,
            cache=cache,
            content_cache=content_cache,
        )

    first = await _crawl()  # stores body + validator
    assert first.status == "success"

    second = await _crawl()  # content-cache revalidation -> origin 304
    assert second.status == "success"
    assert second.classification == "not_modified", second.classification
    assert second.http_status == 304
    # The stored body rides along so a watcher can diff, but the verdict is not_modified.
    assert "STOC" in (second.text or second.markdown or "")
