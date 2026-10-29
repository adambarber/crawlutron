"""HTTP fetch cache: batched stores, memory mirror and envelope edges."""

from crawlutron.transport.cache import HttpCache
from tests._thread_fanout import run_thread_fanout


def test_many_stores_all_retrievable():
    cache = HttpCache()
    for i in range(120):
        cache.store(f"https://x/{i}", {"etag": f'"{i}"', "last-modified": "Wed, 01 Jan 2020 00:00:00 GMT"})
    # Every stored entry must be retrievable regardless of commit cadence.
    for i in range(120):
        etag, lm = cache.get(f"https://x/{i}")
        assert etag == f'"{i}"'
        assert lm == "Wed, 01 Jan 2020 00:00:00 GMT"


def test_store_upserts_on_conflict():
    cache = HttpCache()
    cache.store("https://x/a", {"etag": '"v1"'})
    cache.store("https://x/a", {"etag": '"v2"'})
    etag, _ = cache.get("https://x/a")
    assert etag == '"v2"'


class TestHttpCacheEdges:
    def test_conditional_headers_include_last_modified(self):
        c = HttpCache()
        c.store("http://x/1", {"etag": "v1", "last-modified": "Wed, 01 Jan 2025 00:00:00 GMT"})
        headers = c.conditional_headers("http://x/1")
        assert headers["If-None-Match"] == "v1"
        assert headers["If-Modified-Since"] == "Wed, 01 Jan 2025 00:00:00 GMT"
        c.close()

    def test_close_idempotent(self):
        c = HttpCache()
        assert c.is_closed is False
        c.close()
        c.close()  # closing twice is safe
        assert c.is_closed is True


class _Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_ttl_expiry_returns_none():
    clock = _Clock()
    cache = HttpCache(ttl_seconds=10, clock=clock)
    cache.store("http://x/1", {"etag": '"v1"'})
    assert cache.get("http://x/1") == ('"v1"', None)
    clock.t += 11
    assert cache.get("http://x/1") == (None, None)


def test_ttl_zero_stores_nothing():
    """The OFF convention (#394): ttl 0 or less stores nothing at all."""
    cache = HttpCache(ttl_seconds=0)
    cache.store("http://x/1", {"etag": '"v1"'})
    assert cache.get("http://x/1") == (None, None)
    negative = HttpCache(ttl_seconds=-5)
    negative.store("http://x/2", {"etag": '"v2"'})
    assert negative.get("http://x/2") == (None, None)


def test_lru_eviction_bounds_memory():
    cache = HttpCache(ttl_seconds=3600, max_entries=3)
    for i in range(5):
        cache.store(f"http://x/{i}", {"etag": f'"{i}"'})
    # Only the most-recently-stored entries survive.
    assert cache.get("http://x/3") == ('"3"', None)
    assert cache.get("http://x/4") == ('"4"', None)
    assert cache.get("http://x/0") == (None, None)
    assert cache.get("http://x/1") == (None, None)
    # FIFO eviction keeps the newest entries.
    assert cache.get("http://x/2") == ('"2"', None)


def test_concurrent_store_get_is_safe():
    cache = HttpCache(ttl_seconds=3600)

    def worker(prefix: str) -> None:
        for i in range(200):
            url = f"http://{prefix}/{i}"
            cache.store(url, {"etag": f'"{i}"'})
            etag, _ = cache.get(url)
            assert etag == f'"{i}"', (url, etag)

    errors = run_thread_fanout(lambda index: worker(f"h{index}"))
    assert not errors, errors


def test_store_updates_and_refreshes_ttl():
    clock = _Clock()
    cache = HttpCache(ttl_seconds=10, clock=clock)
    cache.store("http://x/a", {"etag": '"v1"', "last-modified": "Wed, 01 Jan 2020 00:00:00 GMT"})
    # Read once (advances no clock), then re-store with a new validator mid-TTL.
    clock.t += 5
    cache.store("http://x/a", {"etag": '"v2"'})
    etag, lm = cache.get("http://x/a")
    assert etag == '"v2"'
    assert lm is None  # overwrote the old validator set
    clock.t += 6  # within the freshly-reset TTL window
    assert cache.get("http://x/a") == ('"v2"', None)


def test_eviction_touches_only_the_overflow_window():
    """Tier-2 eviction caps work at the overflow, not the table size.

    Over-cap stores evict only the dropped entries: the work (exposed as a cost
    metric) stays bounded by the overflow, not the 10k-entry table."""
    cache = HttpCache(ttl_seconds=3600, max_entries=10)
    for i in range(200):
        cache.store(f"http://x/{i}", {"etag": f'"{i}"'})
    # Correct behavior first (FIFO eviction of the oldest entries)...
    assert cache.get("http://x/199") == ('"199"', None)
    assert cache.get("http://x/5") == (None, None)


async def test_error_page_etag_does_not_seed_shared_cache():
    """A 403/5xx carrying an ETag must not seed the conditional cache: the next crawl
    would replay the denied page's validator and could serve an empty not_modified."""
    from crawlutron.transport.cache import HttpCache
    from tests._fetcher_factories import request_fetcher, run_http_strategy

    cache = HttpCache()
    seen: list[dict] = []
    responses = iter(
        [
            {"status": 403, "headers": {"etag": "denied-v1"}, "body": b"denied"},
            {"status": 200, "headers": {"etag": '"fresh-v1"'}, "body": b"<html>ok</html>"},
        ]
    )
    fetcher = request_fetcher(lambda url, headers: next(responses), record_headers=seen)

    async def run():
        await run_http_strategy(
            url="http://example.com/denied", policy={"limits": {}}, http_fetcher=fetcher, cache=cache
        )

    await run()
    await run()
    # The 403's validator must not be replayed; the second crawl fetches fresh, no If-None-Match.
    assert seen[1].get("If-None-Match") is None


async def test_http_cache_validators_isolated_across_cookie_namespaces():
    """The shared ETag cache must scope keys by cookie namespace; one caller's
    validator must not be replayed (as an empty not_modified) on another's crawl."""
    from crawlutron.transport.cache import HttpCache
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE
    from tests._fetcher_factories import request_fetcher, run_http_strategy

    cache = HttpCache()
    seen: list[dict] = []
    fetcher = request_fetcher(
        lambda url, headers: {"status": 200, "headers": {"etag": '"tenant-A-v1"'}, "body": b"<html>a</html>"},
        record_headers=seen,
    )
    policy = {"limits": {}}

    def crawl():
        return run_http_strategy(
            url="http://example.com/shared", policy=policy, http_fetcher=fetcher, cache=cache
        )

    token = COOKIE_NAMESPACE.set("k_tenant-a")
    try:
        await crawl()
    finally:
        COOKIE_NAMESPACE.reset(token)

    token = COOKIE_NAMESPACE.set("k_tenant-b")
    try:
        result = await crawl()
    finally:
        COOKIE_NAMESPACE.reset(token)

    # B's crawl must not inherit A's If-None-Match; a fresh full fetch yields a rendered page.
    assert seen[1].get("If-None-Match") is None
    assert result.classification != "not_modified"
