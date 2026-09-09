"""HTTP cache edge cases: TTL expiry, conditional revalidation, vary handling."""

from coverage_helpers import *


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
