"""HTTP fetch cache: batched stores, memory mirror and envelope edges."""

from app.crawl.cache import HttpCache
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
        c.close()
        c.close()


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


def test_ttl_zero_never_expires():
    cache = HttpCache(ttl_seconds=0)
    cache.store("http://x/1", {"etag": '"v1"'})
    assert cache.get("http://x/1") == ('"v1"', None)


def test_lru_eviction_bounds_memory():
    cache = HttpCache(ttl_seconds=0, max_entries=3)
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
    cache = HttpCache(ttl_seconds=0)

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

    A recording mapping shows how many keys each eviction draws from the
    iteration order; over-cap stores must stay bounded by the number of
    entries dropped (here one per store), not the 10k-entry table.
    """

    class _RecordingDict(dict):
        def __init__(self, *args):
            super().__init__(*args)
            self.consumed = 0

        def __iter__(self):
            for key in super().__iter__():
                self.consumed += 1
                yield key

    cache = HttpCache(ttl_seconds=0, max_entries=10)
    table = _RecordingDict()
    cache._data = table
    for i in range(200):
        cache.store(f"http://x/{i}", {"etag": f'"{i}"'})
    # Correct behavior first (FIFO eviction of the oldest entries)...
    assert cache.get("http://x/199") == ('"199"', None)
    assert cache.get("http://x/5") == (None, None)
    # ...then the cost bound: an old list(self._data)[:over] snapshot drew the
    # full table (~200k keys) per over-cap store; the bounded window draws at
    # most the overflow plus one per store.
    assert table.consumed <= 200 * 3, table.consumed
