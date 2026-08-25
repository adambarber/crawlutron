"""robots.txt cache must be shared across separate crawl_request calls (#9)."""

from app.crawl.cache import HttpCache
from app.crawl.engine import crawl_request
from app.models import HttpFetchResult


async def test_robots_cache_shared_across_requests():
    import app.crawl.engine as em

    # Reset any module-level robots cache so the test is deterministic.
    cache = getattr(em, "robots_cache", None)
    if isinstance(cache, dict):
        cache.clear()

    calls = {"robots": 0}

    def make_fetcher():
        async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            if url.endswith("/robots.txt"):
                calls["robots"] += 1
                return HttpFetchResult(
                    url=url, final_url=url, status_code=200,
                    headers={"content-type": "text/plain"},
                    body=b"User-agent: *\nDisallow: /secret\n", elapsed_ms=1,
                )
            return HttpFetchResult(
                url=url, final_url=url, status_code=200,
                headers={"content-type": "text/html"},
                body=b"<html><body>hi</body></html>", elapsed_ms=1,
            )

        return fetcher

    payload = {
        "url": "http://x.test/",
        "policy": {
            "strategies": ["HTTP_DECLARED_CRAWLER"],
            "limits": {"timeout_seconds": 10, "max_retries": 0, "respect_robots": True},
        },
    }
    for _ in range(2):
        out = await crawl_request(payload, http_fetcher=make_fetcher(), cache=HttpCache())
        assert out["results"]

    # robots.txt must be fetched once and reused across the two requests.
    assert calls["robots"] == 1, f"robots.txt fetched {calls['robots']} times"
