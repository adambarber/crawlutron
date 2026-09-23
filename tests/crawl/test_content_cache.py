"""Content cache: TTL, disk persistence, memory mirror, byte budget and unlink discipline."""

from __future__ import annotations

import asyncio
import base64
import shutil
import sys

import httpx
import pytest
from _testkit import _temp_cache, make_content_cache_with_entry, make_fetch
from helpers.server import EtagHandler

import app.crawl.content_cache as cc
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.engine import crawl_request
from app.crawl.strategies.fetchers import _default_http_fetcher
from app.crawl.strategies.http import _http_strategy_result
from app.validation import HostPolicy


def _make_etag_test_setup(tmp_path, etag_server):
    """Common setup for etag-based content cache tests."""
    host, port = etag_server.server_address
    url = f"http://127.0.0.1:{port}/"
    policy = {"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}}

    def fetcher(u, headers, timeout, max_content_length=50 * 1024 * 1024):
        return _default_http_fetcher(
            u,
            headers,
            timeout,
            max_content_length,
            policy=HostPolicy.unrestricted(),
        )

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


def test_get_holds_lock_while_updating_memory_mirror(tmp_path, monkeypatch):
    """Sync get() warms the RAM mirror under the lock, exactly like aget().

    ``_remember_memory_locked`` requires the caller to hold ``self._lock``;
    the sync disk-hit path honors the same contract as the async one so the
    mirror/index update stays atomic against concurrent set/aset/get.
    """
    c = ContentCache(cache_dir=tmp_path / "m", default_ttl=3600)
    c.set("k", {"v": 1})
    c._memory_cache.clear()  # force the disk-hit path on the next get()
    held = []
    original = ContentCache._remember_memory_locked

    def recording(self, key, value, expires, size):
        held.append(self._lock.locked())
        return original(self, key, value, expires, size)

    monkeypatch.setattr(ContentCache, "_remember_memory_locked", recording)
    assert c.get("k") == {"v": 1}
    assert held == [True]


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
    from app.crawl.strategies.fetchers import _default_http_fetcher
    from app.crawl.strategies.http import _http_strategy_result

    cache = ContentCache(cache_dir=tmp_path / "hs", default_ttl=3600)
    url = base_url + "/"
    policy = {"strategies": ["HTTP_DECLARED_CRAWLER"], "extraction": "full", "headers": {}}

    calls = {"n": 0}

    async def counting_fetcher(u, headers, timeout, max_content):
        calls["n"] += 1
        return await _default_http_fetcher(
            u,
            headers,
            timeout,
            max_content,
            policy=HostPolicy.unrestricted(),
        )

    r1 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=counting_fetcher,
        content_cache=cache,
        host_policy=HostPolicy.unrestricted(),
    )
    r2 = await _http_strategy_result(
        original_url=url,
        normalized_url=url,
        strategy="HTTP_DECLARED_CRAWLER",
        policy=policy,
        http_fetcher=counting_fetcher,
        content_cache=cache,
        host_policy=HostPolicy.unrestricted(),
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
        host_policy=HostPolicy.unrestricted(),
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
        host_policy=HostPolicy.unrestricted(),
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
        host_policy=HostPolicy.unrestricted(),
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
            host_policy=HostPolicy.unrestricted(),
        )
        assert r2.status == "success"
        assert "changed content" in (r2.markdown or "")
    finally:
        EtagHandler.etag = "v1"
        EtagHandler.body = b"<html><body>etagged content</body></html>"


async def test_crawl_request_serves_recursive_pages_from_content_cache(tmp_path):
    """Recursive crawl_request reuses the content cache for seed AND discovered pages (#5)."""
    from urllib.parse import urlparse

    fetched_pages: list[str] = []

    def site_fetcher(pages):
        def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
            p = urlparse(url).path or "/"
            if p in ("/", "/a"):
                fetched_pages.append(p)
            status, body, ctype = pages.get(p, (404, b"<html></html>", "text/html"))
            if isinstance(body, str):
                body = body.encode()
            return make_fetch(url=url, final_url=url, status=status, ctype=ctype, body=body)

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


class TestContentCacheOverwrite:
    def test_content_cache_set_overwrite(self):
        cache, _ = _temp_cache(ttl=60)
        cache.set("key", {"data": "first"})
        cache.set("key", {"data": "second"})
        assert cache.get("key") == {"data": "second"}


class TestContentCacheCorruptEntry:
    def test_content_cache_get_corrupt_json(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache_path = cache._path_for("testkey")
            cache_path.write_text("{broken json")
            result = cache.get("testkey")
            assert (result) is None

    def test_content_cache_prune_corrupt_entry(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            # Write a corrupt file with the correct hash-based name - prune should skip it
            bad_path = cache._path_for("badkey")
            bad_path.write_text("not valid json")
            clock_vals[0] = 1002.0  # advance past TTL
            removed = cache.prune()
            # Only key1 is expired and removable; corrupt file is skipped
            assert removed == 1

    def test_content_cache_prune_os_error(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = make_content_cache_with_entry(tmpdir, clock=lambda: clock_vals[0])
            clock_vals[0] = 1002.0
            # Replace file with directory to cause OSError on read
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            cache.prune()
            cache_path.rmdir()

    def test_content_cache_clear_os_error(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60)
            cache.set("key1", {"data": "1"})
            cache_path = cache._path_for("key1")
            cache_path.unlink()
            cache_path.mkdir()
            cache.clear()
            cache_path.rmdir()


class TestSharedContentCache:
    def test_engine_shared_content_cache(self):
        from app.crawl.content_cache import ContentCache
        from app.crawl.engine import _get_shared_content_cache

        cache = _get_shared_content_cache()
        assert isinstance(cache, ContentCache)


def test_default_max_entries_is_bounded_not_none():
    """ContentCache has a bounded default max_entries (F6)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600)
        # The default should be a positive integer, not None (which meant unbounded).
        assert c.max_entries is not None
        assert isinstance(c.max_entries, int)
        assert c.max_entries > 0


def test_max_entries_enforces_bound(tmp_path):
    """Cache never exceeds max_entries (F6)."""
    c = ContentCache(cache_dir=tmp_path / "bound", default_ttl=3600, max_entries=3)
    c.set("a", 1)
    c.set("b", 2)
    c.set("c", 3)
    # 3 entries
    assert c.get("a") == 1
    assert c.get("b") == 2
    assert c.get("c") == 3

    # Add 'd' -> one must be evicted to stay at 3
    c.set("d", 4)
    # Total cached entries should be 3
    entries = list((tmp_path / "bound").glob("content.*.json"))
    assert len(entries) == 3
    # All remaining gets should work or return None (evicted)
    # At least one of a,b,c was evicted; d is present
    assert c.get("d") == 4
    # Verify we didn't lose all old entries (at least 2 of a,b,c remain)
    remaining = sum(1 for k in ("a", "b", "c") if c.get(k) is not None)
    assert remaining >= 2


def test_explicit_max_entries_zero_disables_limit():
    """max_entries=0 means no limit (opt-out)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600, max_entries=0)
        # Should not enforce
        for i in range(15000):
            c.set(f"k{i}", i)
        # All should be present (no eviction)
        assert c.get("k0") == 0
        assert c.get("k14999") == 14999


def test_lru_keeps_recently_used_entries():
    """Accessing an entry protects it from next eviction (#5)."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        c = ContentCache(cache_dir=d, default_ttl=3600, max_entries=3)
        c.set("a", 1)
        c.set("b", 2)
        c.set("c", 3)
        # Touch "a" so it becomes most-recently-used.
        assert c.get("a") == 1
        # Push past the cap twice; "a" must survive both evictions.
        c.set("d", 4)
        c.set("e", 5)
        assert c.get("a") == 1
        # And the newest entries are present.
        assert c.get("e") == 5


def _make_cache(tmp_path, **kwargs):
    kwargs.setdefault("default_ttl", 60)
    return ContentCache(str(tmp_path), **kwargs)


def _fail_writes(cache, monkeypatch, *, only_when=None):
    """Patch ``_write_data`` to raise OSError.

    With ``only_when`` set, writes whose serialized data lacks that substring
    still hit the real writer, so a prior entry persists and only the targeted
    write fails. Shared by the write-failure/rollback tests (one seam, not a
    ``boom`` fake cloned per test).
    """
    real_write = cache._write_data

    def boom(path, data):
        if only_when is None or only_when in data:
            raise OSError("disk full")
        real_write(path, data)

    monkeypatch.setattr(cache, "_write_data", boom)


class TestContentCacheByteBudget:
    def test_oversized_entry_is_refused(self, tmp_path):
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        cache.set("big", {"body": "x" * 10_000}, ttl=60)
        assert cache.get("big") is None
        assert list(tmp_path.glob("*.json")) == []

    def test_total_budget_evicts_lru_first(self, tmp_path):
        cache = _make_cache(
            tmp_path,
            max_entries=10_000,
            max_entry_bytes=10_000,
            max_total_bytes=400,
        )
        cache.set("k1", {"body": "a" * 100}, ttl=60)
        cache.set("k2", {"body": "b" * 100}, ttl=60)
        assert cache.get("k1") is not None
        assert cache.get("k2") is not None
        # Third write overflows the budget: k1 (least recently used) goes.
        cache.set("k3", {"body": "c" * 100}, ttl=60)
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None
        on_disk = sum(p.stat().st_size for p in tmp_path.glob("*.json"))
        assert on_disk <= 400

    async def test_aset_enforces_budget(self, tmp_path):
        cache = _make_cache(
            tmp_path,
            max_entries=10_000,
            max_entry_bytes=10_000,
            max_total_bytes=400,
        )
        await cache.aset("k1", {"body": "a" * 100}, ttl=60)
        await cache.aset("k2", {"body": "b" * 100}, ttl=60)
        await cache.aset("k3", {"body": "c" * 100}, ttl=60)
        assert await cache.aget("k1") is None
        assert await cache.aget("k3") is not None

    async def test_aset_oversized_never_written(self, tmp_path, monkeypatch):
        """N1: the async path must refuse oversized entries before any disk
        write -- the payload must not transiently hit disk (crash window).
        Mirrors set(): pre-serialize, size-check under the lock, then write.
        The spy sits on ``_write_data``, the seam the real code uses."""
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        calls: list[str] = []

        real_write = cache._write_data

        def spy_write(path, data):
            calls.append(str(path))
            real_write(path, data)

        monkeypatch.setattr(cache, "_write_data", spy_write)
        await cache.aset("big", {"body": "x" * 10_000}, ttl=60)
        assert calls == []
        assert list(tmp_path.glob("*.json")) == []
        assert await cache.aget("big") is None
        # A small write in the same cache still works.
        await cache.aset("ok", {"body": "y"}, ttl=60)
        assert await cache.aget("ok") is not None

    async def test_aset_accounting_matches_written_bytes(self, tmp_path):
        """N1: aset() byte accounting must equal the exact bytes on disk
        (cross-instance equality is nondeterministic: the envelope embeds
        clock() floats whose repr width varies between writes)."""
        cache_b = _make_cache(tmp_path / "b", max_entry_bytes=10_000)
        await cache_b.aset("same", {"body": "z" * 500}, ttl=60)
        path = cache_b._index["same"]
        assert cache_b._index_sizes["same"] == path.stat().st_size
        assert cache_b._index_total_bytes == path.stat().st_size

    async def test_aset_write_failure_leaves_no_dangling_index(self, tmp_path, monkeypatch):
        """N1: a failed disk write must roll the index entry back so aget()
        does not keep consulting a path that does not exist."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)

        def boom(path, envelope):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "_write_data", boom)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index
        assert cache._index_sizes.get("k", 0) == 0
        assert await cache.aget("k") is None

    def test_prune_repairs_unknown_sizes(self, tmp_path):
        """N1: prune() must reconcile byte accounting for index entries whose
        recorded size is missing (e.g. crash between index insert and
        accounting update) by stat'ing the surviving file."""
        import json as _json

        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("k", {"body": "v" * 300}, ttl=60)
        # Simulate crash residue: index entry survives, its recorded size
        # and the total were lost.
        cache._index_sizes.clear()
        cache._index_total_bytes = 0
        removed = cache.prune()
        assert removed == 0
        path = cache._index["k"]
        assert cache._index_sizes["k"] == path.stat().st_size
        assert cache._index_total_bytes == path.stat().st_size
        assert _json.loads(path.read_text())["value"] == {"body": "v" * 300}

    def test_set_routes_through_write_seam(self, tmp_path, monkeypatch):
        """#1733 twins: the sync write must go through _write_data like aset, so
        a failure is observable/testable at the same seam (a spy on an inline
        atomic_write_text call site is silently vacuous)."""
        cache = _make_cache(tmp_path)

        def boom(path, data):
            raise OSError("disk full")

        monkeypatch.setattr(cache, "_write_data", boom)
        with pytest.raises(OSError):
            cache.set("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index

    async def test_aget_cold_reads_honor_mirror_count_cap(self, tmp_path):
        """#1733 twins: aget's disk-hit mirror enforces the same
        _MEMORY_CACHE_MAX cap as set/aset — otherwise repeated cold reads of
        distinct small keys grow the RAM mirror without bound."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000, max_total_bytes=10_000_000)
        keys = [f"k{i}" for i in range(cache._MEMORY_CACHE_MAX + 50)]
        for key in keys:
            await cache.aset(key, {"body": key}, ttl=60)
        cache._memory_cache.clear()  # simulate a cold process with a warm disk
        for key in keys:
            assert await cache.aget(key) == {"body": key}
        assert len(cache._memory_cache) <= cache._MEMORY_CACHE_MAX
        for key in keys[-cache._MEMORY_CACHE_MAX :]:
            assert key in cache._memory_cache  # the cap must evict OLDEST, not block new

    def test_get_disk_hit_mirrors_small_value(self, tmp_path):
        """#1733 twins: a hot URL read through the SYNC path warms the RAM
        mirror exactly like the async path (the accelerator contract is
        method-independent)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("hot", {"body": "small"}, ttl=60)
        cache._memory_cache.clear()
        assert cache.get("hot") == {"body": "small"}
        assert "hot" in cache._memory_cache

    async def test_aset_write_failure_rolls_back_stale_mirror(self, tmp_path, monkeypatch):
        """#1733 twins: when the async disk write fails with a surviving older
        entry, the in-memory mirror must not keep serving the value that was
        never persisted — rollback covers RAM, not just the index."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("k", {"body": "old"}, ttl=60)
        _fail_writes(cache, monkeypatch, only_when="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new"}, ttl=60)
        assert await cache.aget("k") == {"body": "old"}
        assert cache.get("k") == {"body": "old"}

    async def test_aset_new_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """L1: a failed write of a NEW key must not move _index_total_bytes.

        Bytes enter the total only at commit (after a successful write), so the
        old rollback's `total -= size` decremented bytes that were never added;
        the max(0, ...) clamp only hid it when the cache was otherwise empty."
        """
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("keep", {"body": "v" * 200}, ttl=60)  # counts once, stays
        total_before = cache._index_total_bytes
        assert total_before == sum(cache._index_sizes.values()) > 0

        _fail_writes(cache, monkeypatch)
        with pytest.raises(OSError):
            await cache.aset("ghost", {"body": "g" * 300}, ttl=60)
        assert "ghost" not in cache._index
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())

    async def test_aset_existing_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """L1: an existing key whose re-write fails keeps its prior bytes counted
        (the old rollback applied a spurious `prior_size - size` delta)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("k", {"body": "old"}, ttl=60)
        total_before = cache._index_total_bytes
        assert cache._index_sizes["k"] > 0
        _fail_writes(cache, monkeypatch, only_when="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new" * 100}, ttl=60)
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())
        assert await cache.aget("k") == {"body": "old"}


class TestExpiredEntryFreesByteAccounting:
    """reading an expired (or unreadable) entry popped ``_index`` but left
    ``_index_sizes``/``_index_total_bytes`` untouched. The phantom bytes keep
    ``_over_limits()`` true, so the next ``set()``'s ``_enforce_limits`` evicts
    LIVE LRU entries it should not -- a sustained self-inflicted cache wipe-out
    on a TTL-busy cache.
    """

    def test_expired_read_frees_byte_accounting(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            # Tight budget: two entries fit, three do not.
            max_total_bytes=10_000,
        )

        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        size_a = cache._index_sizes["a"]
        total_before = cache._index_total_bytes
        assert total_before > 0

        # Advance past the TTL and read the now-expired entry.
        now[0] = 2000.0
        assert cache.get("a") is None, "expired entry must read as None"

        # The expired read must free A's byte accounting (via _drop_locked),
        # not just the index entry.
        assert cache._index_total_bytes == total_before - size_a, (
            f"phantom bytes: total {cache._index_total_bytes} != "
            f"{total_before - size_a} (expired entry's {size_a} bytes still counted)"
        )

    def test_set_after_expired_read_does_not_evict_live_entry(self, tmp_path):
        from app.crawl.content_cache import ContentCache

        now = [1000.0]
        cache = ContentCache(
            tmp_path / "cache",
            default_ttl=10,
            clock=lambda: now[0],
            max_entries=100,
            max_total_bytes=10_000,
        )
        cache.set("a", "x" * 100)
        cache.set("b", "x" * 100)
        # Budget for exactly two entries.
        unit = cache._index_sizes["b"]
        cache.max_total_bytes = 2 * unit + unit // 2

        now[0] = 2000.0
        assert cache.get("a") is None  # expired read frees A's bytes (or not)
        now[0] = 1000.0  # keep B and C unexpired for the assertions

        cache.set("c", "x" * 100)
        # B is live and must not be sacrificed to phantom A bytes. Clear the
        # RAM mirror so the assertion reads the disk-backed index, not RAM.
        cache._memory_cache.clear()
        assert cache.get("b") == "x" * 100, "live LRU entry evicted by phantom bytes"


def _make(tmp_path, clock=1000.0):
    shutil.rmtree(tmp_path, ignore_errors=True)
    return cc.ContentCache(tmp_path, namespace="mem", clock=lambda: clock, max_entries=10000)


def test_small_value_lives_in_memory(tmp_path):
    c = _make(tmp_path)
    c.set("a", {"x": 1})
    assert c.get("a") == {"x": 1}
    assert "a" in c._memory_cache


def test_large_value_not_in_memory(tmp_path):
    c = _make(tmp_path)
    big = "z" * (2 * 1024 * 1024)  # 2MB string -> shallow size exceeds 1MB
    c.set("big", big)
    assert "big" not in c._memory_cache
    # Still resolvable from disk.
    assert c.get("big") == big


def test_memory_ttl_honored(tmp_path):
    c = _make(tmp_path)
    c.set("k", "v", ttl=60)  # expires at 1060
    assert c.get("k") == "v"
    c.clock = lambda: 2000.0
    assert c.get("k") is None
    # Expired entry evicted from memory cache.
    assert "k" not in c._memory_cache


def test_memory_lru_cap(tmp_path):
    c = _make(tmp_path)
    for i in range(150):
        c.set(f"k{i}", i)
    assert len(c._memory_cache) <= c._MEMORY_CACHE_MAX
    # Oldest evicted from memory, but still resolvable from disk.
    assert "k0" not in c._memory_cache
    assert "k149" in c._memory_cache
    assert c.get("k0") == 0


def test_clear_clears_memory(tmp_path):
    c = _make(tmp_path)
    c.set("a", 1)
    assert "a" in c._memory_cache
    c.clear()
    assert len(c._memory_cache) == 0


class TestContentCacheEntryLifecycle:
    """Corrupted entries, expiry, prune/clear and async disk writes"""

    def test_content_cache_get_invalid_json(self):
        import pathlib
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache_path = pathlib.Path(tmpdir) / "content.testkey.json"
            cache_path.write_text("not valid json")
            result = cache.get("testkey")
            assert (result) is None

    def test_content_cache_get_expired(self):
        cache, clock_vals = _temp_cache(ttl=1)
        cache.set("key", {"data": "value"})
        clock_vals[0] = 1002.0
        assert (cache.get("key")) is None

    def test_content_cache_prune(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            clock_vals = [1000.0]
            cache = ContentCache(tmpdir, default_ttl=1, clock=lambda: clock_vals[0])
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            clock_vals[0] = 1002.0
            removed = cache.prune()
            assert removed == 2
            assert (cache.get("key1")) is None
            assert (cache.get("key2")) is None

    def test_content_cache_clear(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir)
            cache.set("key1", {"data": "1"})
            cache.set("key2", {"data": "2"})
            removed = cache.clear()
            assert removed == 2
            assert (cache.get("key1")) is None
            assert (cache.get("key2")) is None

    async def test_aset_aget_roundtrip(self):
        """Async disk cache stores and retrieves, including a large (HTML-sized)
        value that hits the on-disk path."""
        import pathlib
        import tempfile

        from app.crawl.content_cache import ContentCache

        d = tempfile.mkdtemp()
        cache = ContentCache(cache_dir=pathlib.Path(d), default_ttl=60)
        big = "<html>" + ("x" * 2_000_000) + "</html>"
        await cache.aset("k1", big)
        # RAM mirror only keeps <1MiB, so this exercises the disk read path.
        got = await cache.aget("k1")
        assert got == big

    async def test_aset_writes_off_event_loop(self):
        """The disk write (json.dumps of a large value) runs in a worker thread."""
        import pathlib
        import tempfile
        import threading

        from app.crawl.content_cache import ContentCache

        d = tempfile.mkdtemp()
        cache = ContentCache(cache_dir=pathlib.Path(d), default_ttl=60)
        seen = {}

        def spy_write(path, data):
            seen["thread"] = threading.current_thread()
            path.write_text(data)

        cache._write_data = spy_write
        big = "y" * 2_000_000
        await cache.aset("k2", big)
        assert seen, "write never happened"
        assert seen["thread"] is not threading.current_thread()


class TestCachedAtReadsMetadataWithoutDeserializingTheValue:
    """P6: cached_at_of must not deserialize the whole envelope on every call.

    Sitemap freshness checks call this for many keys per crawl, and each
    envelope can hold a multi-MB body. Reading the value bytes off disk (and
    JSON-parsing them) to answer a timestamp question makes freshness O(bytes)
    instead of O(keys). The timestamp is now answered from a metadata mirror
    that is warmed on set and on the first read, with _read_envelope remaining
    the single chokepoint for full reads and for every state mutation.
    """

    def test_cached_at_after_set_reads_no_envelope(self):
        cache, _ = _temp_cache(ttl=60)
        cache.set("k", {"big": "x" * 100})
        cache.set("k", {"big": "y" * 100})  # ensure the mirror is refreshed

        calls = []
        original = cache._read_envelope

        def counting(key):
            calls.append(key)
            return original(key)

        cache._read_envelope = counting
        try:
            assert cache.cached_at_of("k") is not None
            assert cache.cached_at_of("k") is not None
        finally:
            cache._read_envelope = original
        assert calls == [], f"freshness check deserialized the envelope: {calls}"

    def test_cached_at_value_matches_the_envelope(self):
        cache, clock_vals = _temp_cache(ttl=60)
        cache.set("k", {"data": "value"})
        expected = clock_vals[0]
        assert cache.cached_at_of("k") == expected

    def test_cached_at_of_a_cold_on_disk_entry_answers_and_then_warms(self):
        """A warm restart (fresh instance, same directory) still answers
        correctly from disk, and the read pays for itself on later calls."""
        import tempfile

        from app.crawl.content_cache import ContentCache

        with tempfile.TemporaryDirectory() as tmpdir:
            clock = [1000.0]
            cache = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0])
            cache.set("k", {"data": "value"})
            cached_at = cache.cached_at_of("k")

            reopened = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0])
            calls = []
            original = reopened._read_envelope

            def counting(key):
                calls.append(key)
                return original(key)

            reopened._read_envelope = counting
            try:
                # Cold mirror: one full read is unavoidable and correct.
                assert reopened.cached_at_of("k") == cached_at
                # Now warm: later freshness checks never touch the envelope.
                assert reopened.cached_at_of("k") == cached_at
            finally:
                reopened._read_envelope = original
        assert len(calls) == 1, f"expected one cold read, got {calls}"

    def test_expired_entry_reports_none_through_the_chokepoint(self):
        """An expired mirror entry must still be expired and dropped by the
        shared chokepoint, not silently answered from stale metadata."""
        cache, clock_vals = _temp_cache(ttl=10)
        cache.set("k", {"data": "value"})
        assert cache.cached_at_of("k") is not None
        clock_vals[0] += 1000.0
        assert cache.cached_at_of("k") is None
        # The expired entry is gone from the index, not just unanswered.
        assert "k" not in cache._index

    def test_evicted_entry_is_not_answered_from_stale_metadata(self):
        import tempfile

        from app.crawl.content_cache import ContentCache

        clock = [1000.0]
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = ContentCache(tmpdir, default_ttl=60, clock=lambda: clock[0], max_entries=1)
            cache.set("k1", {"data": "one"})
            assert cache.cached_at_of("k1") is not None
            # Evict k1 via the LRU cap.
            cache.set("k2", {"data": "two"})
            assert "k1" not in cache._index
            assert cache.cached_at_of("k1") is None
            assert cache.cached_at_of("k2") is not None


class TestContentCacheUnlinkDiscipline:
    def test_eviction_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """Eviction mutates the index/byte accounting under the lock and unlinks
        the doomed files only after releasing it: the event-loop thread
        contends on this lock, so disk I/O must never hold it."""
        import app.crawl.content_cache as cc_mod

        cache = cc_mod.ContentCache(tmp_path, max_entries=2, max_total_bytes=None, default_ttl=0)
        real_unlink = cc_mod.Path.unlink

        def _unlink_unlocked(self, *args, **kwargs):
            # Only cache-entry files matter; atomic_write_text unlinks its own
            # .tmp file under the lock and that is not disk eviction I/O.
            if self.parent == cache.cache_dir and not self.name.startswith("."):
                assert not cache._lock.locked(), "unlink ran while holding the cache lock"
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(cc_mod.Path, "unlink", _unlink_unlocked)
        # Over-cap by one: the oldest entry is evicted and its file unlinked.
        cache.set(cache.make_key("a"), {"v": 1})
        cache.set(cache.make_key("b"), {"v": 2})
        cache.set(cache.make_key("c"), {"v": 3})

        assert cache.get(cache.make_key("a")) is None
        assert cache.get(cache.make_key("c")) == {"v": 3}

    def test_expiry_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """The TTL-expiry path in _read_envelope unlinks outside the lock too."""
        import app.crawl.content_cache as cc_mod

        cache, clock_vals = _temp_cache(ttl=1)
        real_unlink = cc_mod.Path.unlink
        seen = {"locked": False}

        def _unlink_unlocked(self, *args, **kwargs):
            if self.parent == cache.cache_dir and not self.name.startswith("."):
                if cache._lock.locked():
                    seen["locked"] = True
            return real_unlink(self, *args, **kwargs)

        monkeypatch.setattr(cc_mod.Path, "unlink", _unlink_unlocked)
        cache.set("key", {"data": "value"})
        clock_vals[0] = 1002.0
        assert cache.get("key") is None
        assert not seen["locked"], "expiry unlink ran while holding the cache lock"

    def test_re_set_key_survives_its_stale_eviction_unlink(self, tmp_path):
        """A key re-set after its eviction reuses the same deterministic path.

        The deferred unlink must spare a file that has become live again,
        otherwise the fresh write is destroyed and only a self-healing cache
        miss hides the loss.
        """
        import app.crawl.content_cache as cc_mod

        cache = cc_mod.ContentCache(tmp_path, max_entries=1, max_total_bytes=None, default_ttl=0)
        key = cache.make_key("hot")
        cache.set(key, {"v": 1})

        # Evict exactly as _enforce_limits does, but leave the unlink pending
        # so the re-registration can land inside the deferral window.
        doomed: list[tuple[str, cc_mod.Path]] = []
        with cache._lock:
            cache._drop_locked(key, doomed)

        # The key is written again to the same deterministic path.
        cache.set(key, {"v": "fresh"})
        cache._unlink_doomed(doomed)

        assert cache.get(key) == {"v": "fresh"}
