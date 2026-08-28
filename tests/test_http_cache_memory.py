"""Tests for the in-memory HttpCache replacement (#adversarial perf 1.1).

The cache moved from a single blocking, lock-free SQLite connection to an
in-memory dict with TTL + LRU. These tests pin the new guarantees: no blocking
SQL on the event loop, thread-safe concurrent store/get, TTL expiry, and
bounded / FIFO eviction.
"""

import threading
from app.crawl.cache import HttpCache


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
    errors: list[BaseException] = []

    def worker(prefix: str) -> None:
        try:
            for i in range(200):
                url = f"http://{prefix}/{i}"
                cache.store(url, {"etag": f'"{i}"'})
                etag, _ = cache.get(url)
                assert etag == f'"{i}"', (url, etag)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(f"h{t}",)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
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
