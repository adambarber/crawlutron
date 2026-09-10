"""Strict-TDD tests for the in-container disk ContentCache (no sidecar).

Real-behavior: a counting wrapper around the real default fetcher proves the
cache short-circuits the second crawl so the upstream server is hit only once.
No mocks of crawler collaborators.
"""

from __future__ import annotations

import asyncio
import base64
import sys

import httpx
from helpers.server import EtagHandler

from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.http import _default_http_fetcher, _http_strategy_result

# --------------------------- unit: pure cache behavior ---------------------------


def _make_etag_test_setup(tmp_path, etag_server):
    """Common setup for etag-based content cache tests."""
    host, port = etag_server.server_address
    url = f"http://127.0.0.1:{port}/"
    policy = {"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}}

    def fetcher(u, headers, timeout, max_content_length=50 * 1024 * 1024):
        return _default_http_fetcher(u, headers, timeout, max_content_length)

    cache = HttpCache()
    content_cache = ContentCache(tmp_path)
    return url, policy, fetcher, cache, content_cache


def test_set_get_roundtrip(tmp_path):
    c = ContentCache(cache_dir=tmp_path / "u", default_ttl=3600)
    assert c.get("missing") is None
    c.set("k", {"a": 1, "b": "x"})
    assert c.get("k") == {"a": 1, "b": "x"}


def test_disk_persistence_across_instances(tmp_path):
    c1 = ContentCache(cache_dir=tmp_path / "d", default_ttl=3600)
    c1.set("k", {"x": 1})
    # New instance, same directory -> must read from disk (no sidecar/redis).
    c2 = ContentCache(cache_dir=tmp_path / "d", default_ttl=3600)
    assert c2.get("k") == {"x": 1}


def test_ttl_expiry_via_injected_clock(tmp_path):
    clock = {"t": 1000.0}
    c = ContentCache(cache_dir=tmp_path / "t", default_ttl=100, clock=lambda: clock["t"])
    c.set("k", "v")
    assert c.get("k") == "v"
    clock["t"] = 1000.0 + 101  # past TTL
    assert c.get("k") is None


def test_zero_ttl_means_no_expiry(tmp_path):
    clock = {"t": 0.0}
    c = ContentCache(cache_dir=tmp_path / "z", default_ttl=0, clock=lambda: clock["t"])
    c.set("k", "v")
    clock["t"] = 10**9
    assert c.get("k") == "v"


def test_prune_removes_only_expired(tmp_path):
    clock = {"t": 0.0}
    c = ContentCache(cache_dir=tmp_path / "p", default_ttl=10, clock=lambda: clock["t"])
    c.set("a", 1)
    c.set("b", 2)
    clock["t"] = 20.0  # a,b expired (expire at 10)
    c.set("c", 3)  # expires at 30, still fresh at t=20
    assert c.prune() == 2
    assert c.get("c") == 3
    assert c.get("a") is None


# --------------------------- integration: real server, no mocks ---------------------------


async def _fetch_via_cache(url: str, cache: ContentCache):
    key = cache.make_key(url)
    hit = cache.get(key)
    if hit is not None:
        return hit
    async with httpx.AsyncClient() as client:
        r = await client.get(url)
    val = {
        "status_code": r.status_code,
        "headers": dict(r.headers),
        "body_b64": base64.b64encode(r.content).decode("ascii"),
        "final_url": str(r.url),
        "elapsed_ms": 0,
    }
    cache.set(key, val)
    return val


def test_cache_short_circuits_real_fetch(counting_server, tmp_path):
    url = f"http://127.0.0.1:{counting_server.server_address[1]}/page"
    cache = ContentCache(cache_dir=tmp_path / "int", default_ttl=3600)
    asyncio.run(_fetch_via_cache(url, cache))
    asyncio.run(_fetch_via_cache(url, cache))
    assert counting_server.hits == 1  # second call served from disk cache


async def test_http_strategy_result_uses_content_cache(base_url, tmp_path):
    from app.crawl.strategies.http import _default_http_fetcher, _http_strategy_result

    cache = ContentCache(cache_dir=tmp_path / "hs", default_ttl=3600)
    url = base_url + "/"
    policy = {"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}}

    calls = {"n": 0}

    async def counting_fetcher(u, headers, timeout, max_content):
        calls["n"] += 1
        return await _default_http_fetcher(u, headers, timeout, max_content)

    r1 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=counting_fetcher,
        content_cache=cache,
    )
    r2 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=counting_fetcher,
        content_cache=cache,
    )
    assert r1.status == "success" and r2.status == "success"
    assert calls["n"] == 1  # cache prevented the second real fetch


async def test_content_cache_revalidates_etagged_url(etag_server, tmp_path):
    """An etagged URL is revalidated (conditional GET) and served from cache on 304 (#5)."""
    from app.crawl.strategies.http import _http_strategy_result

    url, policy, fetcher, cache, content_cache = _make_etag_test_setup(tmp_path, etag_server)
    # First crawl: stores 200 + etag.
    r1 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=fetcher,
        cache=cache,
        content_cache=content_cache,
    )
    assert r1.status == "success"
    assert etag_server.hits == 1
    # Second crawl: content HIT + validator present -> conditional revalidation (304) -> cache served.
    r2 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=fetcher,
        cache=cache,
        content_cache=content_cache,
    )
    assert r2.status == "success"
    # Server was re-hit once for the conditional GET (304, no body re-download).
    assert etag_server.hits == 2
    assert "etagged content" in (r2.markdown or "")


async def test_content_cache_etag_change_avoids_stale(etag_server, tmp_path):
    """If the etag changed, revalidation returns 200 and fresh content is served (#5)."""
    url, policy, fetcher, cache, content_cache = _make_etag_test_setup(tmp_path, etag_server)
    await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=fetcher,
        cache=cache,
        content_cache=content_cache,
    )
    # Simulate the upstream page changing its etag + body.
    try:
        EtagHandler.etag = "v2"
        EtagHandler.body = b"<html><body>changed content</body></html>"
        r2 = await _http_strategy_result(
            original_url=url,
            normalized_url=url,
            strategy="HTTP_DECLARED_CRAWLER",
            policy=policy,
            http_fetcher=fetcher,
            cache=cache,
            content_cache=content_cache,
        )
        assert r2.status == "success"
        assert "changed content" in (r2.markdown or "")
    finally:
        EtagHandler.etag = "v1"
        EtagHandler.body = b"<html><body>etagged content</body></html>"


async def test_crawl_request_serves_recursive_pages_from_content_cache(tmp_path):
    """Recursive crawl_request reuses the content cache for seed AND discovered pages (#5)."""
    from urllib.parse import urlparse

    from app.crawl.strategies.http import HttpFetchResult

    fetched_pages: list[str] = []

    def site_fetcher(pages):
        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            p = urlparse(url).path or "/"
            if p in ("/", "/a"):
                fetched_pages.append(p)
            status, body, ctype = pages.get(p, (404, b"<html></html>", "text/html"))
            if isinstance(body, str):
                body = body.encode()
            return HttpFetchResult(
                url=url,
                final_url=url,
                status_code=status,
                headers={"content-type": ctype},
                body=body,
                elapsed_ms=1,
            )

        return fetcher

    pages = {
        "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
        "/a": (200, "<html>a</html>", "text/html"),
    }
    payload = {"url": "http://example.com/", "policy": {"limits": {"max_depth": 1, "max_pages": 5}}}
    cache = HttpCache()
    content_cache = ContentCache(tmp_path)
    await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=cache, content_cache=content_cache)
    first = list(fetched_pages)
    await crawl_request(payload, http_fetcher=site_fetcher(pages), cache=cache, content_cache=content_cache)
    second = list(fetched_pages)[len(first) :]  # only the 2nd crawl's page fetches
    assert first == ["/", "/a"], first
    # Both the seed and the discovered child must be served from the content cache.
    assert second == [], f"recursive pages should be served from content cache, re-fetched: {second}"


async def test_mirror_gate_uses_serialized_size_not_shallow_getsizeof(tmp_path):
    """Shallow sys.getsizeof on a dict sees only the container (~200 B), so a
    multi-MB serialized entry passed the old mirror gate and landed in RAM
    (final-review #1: worst case _MEMORY_CACHE_MAX x full-size entries)."""
    cache = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    fat = {"body": "x" * (cache._MEMORY_CACHE_MAX_SIZE + 1024)}
    assert sys.getsizeof(fat) < cache._MEMORY_CACHE_MAX_SIZE  # shallow size: tiny

    await cache.aset("fat-key", fat)
    assert cache._read_envelope("fat-key") is not None  # disk: written
    assert "fat-key" not in cache._memory_cache  # RAM mirror: must skip


async def test_mirror_gate_small_value_still_mirrors(tmp_path):
    cache = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    await cache.aset("small-key", {"body": "tiny"})
    assert "small-key" in cache._memory_cache


async def test_cold_aget_does_not_mirror_oversized_envelope(tmp_path):
    """H2: after a warm restart ``_index_sizes`` is empty for pre-existing files,
    so ``aget`` must gate the RAM mirror on the actual envelope bytes (stat),
    not ``_index_sizes.get(key, 0)`` -- which defaults to 0 and lets a multi-MB
    cold read into the small-value mirror (the RAM exhaustion path the
    serialized-size gate was built to close)."""
    fat = {"body": "x" * (1024 * 1024 + 1024)}  # > _MEMORY_CACHE_MAX_SIZE
    writer = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    await writer.aset("fat-key", fat)
    assert "fat-key" not in writer._memory_cache  # write gate already refuses

    # Fresh instance over the same dir: cold index, pre-existing disk file.
    reader = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    assert reader._index_sizes.get("fat-key") is None  # truly cold
    got = await reader.aget("fat-key")
    assert got is not None  # served from disk
    assert "fat-key" not in reader._memory_cache  # must NOT land in RAM mirror


async def test_cold_aget_still_mirrors_small_envelope(tmp_path):
    """Parity: a genuinely small cold read keeps the RAM-mirror benefit."""
    writer = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    await writer.aset("small-key", {"body": "tiny"})
    reader = ContentCache(tmp_path, max_entry_bytes=4 * 1024 * 1024, max_total_bytes=64 * 1024 * 1024)
    assert reader._index_sizes.get("small-key") is None
    got = await reader.aget("small-key")
    assert got == {"body": "tiny"}
    assert "small-key" in reader._memory_cache  # small: mirror is correct
