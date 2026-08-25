"""HttpCache.store batches commits instead of committing per write (#12)."""

import tempfile

from app.crawl.cache import HttpCache


def test_many_stores_all_retrievable():
    with tempfile.TemporaryDirectory() as d:
        cache = HttpCache(db_path=f"{d}/hc.sqlite")
        for i in range(120):
            cache.store(f"https://x/{i}", {"etag": f'"{i}"', "last-modified": "Wed, 01 Jan 2020 00:00:00 GMT"})
        # Every stored entry must be retrievable regardless of commit cadence.
        for i in range(120):
            etag, lm = cache.get(f"https://x/{i}")
            assert etag == f'"{i}"'
            assert lm == "Wed, 01 Jan 2020 00:00:00 GMT"


def test_store_upserts_on_conflict():
    with tempfile.TemporaryDirectory() as d:
        cache = HttpCache(db_path=f"{d}/hc2.sqlite")
        cache.store("https://x/a", {"etag": '"v1"'})
        cache.store("https://x/a", {"etag": '"v2"'})
        etag, _ = cache.get("https://x/a")
        assert etag == '"v2"'
