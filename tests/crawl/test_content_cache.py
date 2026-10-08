"""Content cache: TTL, disk persistence, memory mirror, byte budget and unlink discipline."""

from __future__ import annotations

import contextlib
import json
import shutil
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import app.crawl.content_cache as cc
from app.crawl.cache import HttpCache
from app.crawl.content_cache import ContentCache
from app.crawl.strategies.fetchers import default_http_fetcher
from app.models.normalized_url import NormalizedUrl
from app.validation import HostPolicy
from app.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._fetcher_factories import (
    clocked_cache,
    crawled_results,
    make_expiring_cache,
    make_seeded_cache,
    run_http_strategy,
    tap_fetcher,
)
from tests.helpers.server import EtagHandler


@contextlib.contextmanager
def scoped_cache(**settings) -> Iterator[ContentCache]:
    """A ContentCache over a throwaway directory, removed when the block ends.

    ``with tempfile.TemporaryDirectory() as d: cache = ContentCache(d,...)``
    was the arrange at a dozen sites in this file, differing only in the
    settings; owning it here means a test cannot leave a cache writing into a
    directory it meant to clean up. The directory itself is ``cache.cache_dir``
    for the two sites that need to inspect the on-disk files.

    Two kinds of site sit outside it on purpose, because the block is not
    really this shape: one opens a *second* cache over the same directory (a
    warm restart), which needs the directory rather than a cache; the other
    seeds its entry through ``make_content_cache_with_entry``, which returns
    the clock handle the test then advances.

    Settings are passed through, never restated. The byte caps in particular
    used to be written out here as literal numbers, and one of those literals
    had drifted to a value eight times smaller than the production default --
    a test that kept passing while testing a cap nothing shipped with.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield ContentCache(tmpdir, **settings)


def etag_case(tmp_path, etag_server):
    """``(url, policy, fetcher, cache, content_cache)`` for the etag server's URL.

    The real fetcher against the real local server, with a tap over it.
    Only the wiring is test-owned; nothing here answers HTTP.
    """
    port = etag_server.server_address[1]
    return (
        f"http://127.0.0.1:{port}/",
        {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": "full", "headers": {}},
        tap_fetcher(default_http_fetcher),
        HttpCache(),
        ContentCache(tmp_path),
    )


def cached_strategy_runner(url, policy, fetcher, cache=None, content_cache=None):
    """The HTTP strategy over one URL and the two caches, handed back unrun.

    "What the *second* crawl did" is the subject of these tests, and one of them
    changes the origin's answer in between, so the arrangement is owned here and
    the moment of each run is the test's. ``run_http_strategy`` drops a cache
    that was not given, so passing neither is a strategy run with no caching.
    """

    async def run():
        return await run_http_strategy(url, policy, fetcher, cache=cache, content_cache=content_cache)

    return run


@contextlib.contextmanager
def watching_entry_unlinks(monkeypatch, cache) -> Iterator[list[bool]]:
    """Record, per cache-entry unlink, whether the cache lock was held.

    Scoped to the cache directory's own entry files on purpose:
    ``atomic_write_text`` unlinks its own ``.tmp`` file *under* the lock, so an
    unfiltered watch reports the writer's temp-file cleanup as an eviction. The
    teardown is the ``monkeypatch`` fixture's, which restores the real unlink.
    """
    real_unlink = cc.Path.unlink
    held: list[bool] = []

    def watched(self, *args, **kwargs):
        if self.parent == cache.cache_dir and not self.name.startswith("."):
            held.append(cache._lock.locked())
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(cc.Path, "unlink", watched)
    yield held


@contextlib.contextmanager
def entry_replaced_by_directory(cache, key) -> Iterator[Any]:
    """Swap an entry's file for a directory, so reading it raises OSError.

    A file swapped for a directory is the only way to make a read fail without
    patching the reader itself, and the swap has to be undone -- otherwise the
    directory the cache is writing into is left dirty for the next test.
    """
    path = cache._path_for(key)
    path.unlink()
    path.mkdir()
    try:
        yield path
    finally:
        path.rmdir()


@contextlib.contextmanager
def watching_envelope_reads(cache) -> Iterator[list[str]]:
    """Record the keys that reach the full-envelope read, then restore the seam.

    ``cached_at_of`` is contractually a metadata question; this is what proves a
    test proved it, and the patch/restore pair was re-spelled at each site.
    """
    calls: list[str] = []
    original = cache._read_envelope

    def counting(key):
        calls.append(key)
        return original(key)

    cache._read_envelope = counting
    try:
        yield calls
    finally:
        cache._read_envelope = original


@contextlib.contextmanager
def etag_serving(etag: str, body: bytes) -> Iterator[None]:
    """Make the etag server answer with a new validator and body, then restore.

    The restore values are read back off the handler rather than re-spelled: a
    copy of them drifts the moment the fixture's default body changes, and the
    drift is invisible -- the next test just talks to a server nobody else is
    talking to.
    """
    original = (EtagHandler.etag, EtagHandler.body)
    EtagHandler.etag, EtagHandler.body = etag, body
    try:
        yield
    finally:
        EtagHandler.etag, EtagHandler.body = original


def watch_writes(cache, *, calls=None, on_write=None, fail=None):
    """Point the cache's ``write_data`` seam at a watcher so a test can watch,
    or fail, the cache's disk writes.

    One seam, three questions: ``calls`` records the paths that reached disk (an
    oversized entry must never transiently be one), ``on_write`` observes each
    write -- which is how the off-loop test sees which thread ran it -- and
    ``fail`` makes the write raise, either always (``True``) or only for data
    carrying a substring, so a prior entry survives to be read back. Each of
    those was its own inline closure over the same private method.
    """
    real_write = cache.write_data

    def seam(path, data):
        if calls is not None:
            calls.append(str(path))
        if on_write is not None:
            on_write(path, data)
        if fail is not None and (fail is True or fail in data):
            raise OSError("disk full")
        real_write(path, data)

    cache.write_data = seam


def test_set_get_roundtrip():
    with scoped_cache(default_ttl=3600) as c:
        assert c.get("missing") is None
        c.set("k", {"a": 1, "b": "x"})
        assert c.get("k") == {"a": 1, "b": "x"}


def test_disk_persistence_across_instances(tmp_path):
    first = _make_cache(tmp_path, default_ttl=3600)
    first.set("k", {"x": 1})
    # A new instance over the same directory -> must read from disk (no sidecar/redis).
    assert reopen_cache(tmp_path, default_ttl=3600).get("k") == {"x": 1}


def test_ttl_expiry_via_injected_clock(tmp_path):
    c, clock = clocked_cache(tmp_path / "t", default_ttl=100)
    c.set("k", "v")
    assert c.get("k") == "v"
    clock[0] = 1000.0 + 101  # past TTL
    assert c.get("k") is None


def test_zero_ttl_means_no_expiry(tmp_path):
    c, clock = clocked_cache(tmp_path / "z", default_ttl=0, clock_start=0.0)
    c.set("k", "v")
    clock[0] = 10**9
    assert c.get("k") == "v"


def test_prune_removes_only_expired(tmp_path):
    c, clock = clocked_cache(tmp_path / "p", default_ttl=10, clock_start=0.0)
    c.set("a", 1)
    c.set("b", 2)
    clock[0] = 20.0  # a,b expired (expire at 10)
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


def real_fetcher(calls: list[str] | None = None):
    """The production HTTP fetcher, optionally under a tap that records URLs.

    Every integration below is about what the *real* transport did, so the
    fetcher is production's own; only the observation is test-owned.
    """
    return default_http_fetcher if calls is None else tap_fetcher(default_http_fetcher, calls)


async def test_cache_short_circuits_real_fetch(counting_server, tmp_path):
    url = f"http://127.0.0.1:{counting_server.server_address[1]}/page"
    run = cached_strategy_runner(
        url,
        {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": "full", "headers": {}},
        real_fetcher(),
        content_cache=ContentCache(tmp_path),
    )
    await run()
    await run()
    assert counting_server.hits == 1  # the second call was served from disk cache


async def test_http_strategy_result_uses_content_cache(base_url, tmp_path):
    fetched: list[str] = []
    run = cached_strategy_runner(
        f"{base_url}/",
        {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": "full", "headers": {}},
        real_fetcher(fetched),
        content_cache=ContentCache(cache_dir=tmp_path / "hs", default_ttl=3600),
    )
    first = await run()
    second = await run()
    assert first.status == "success" and second.status == "success"
    assert fetched == [
        NormalizedUrl(f"{base_url}/", policy=HostPolicy.unrestricted()).raw
    ]  # the cache prevented the second real fetch


async def test_content_cache_revalidates_etagged_url(etag_server, tmp_path):
    """An etagged URL is revalidated (conditional GET) and served from cache on 304."""
    run = cached_strategy_runner(*etag_case(tmp_path, etag_server))
    first = await run()
    assert first.status == "success"
    assert etag_server.hits == 1
    # Content HIT + validator present -> conditional revalidation (304) -> cache served.
    second = await run()
    assert second.status == "success"
    # Re-hit once for the conditional GET (304, no body re-download).
    assert etag_server.hits == 2
    assert "etagged content" in (second.markdown or "")


async def test_content_cache_etag_change_avoids_stale(etag_server, tmp_path):
    """If the etag changed, revalidation returns 200 and fresh content is served."""
    run = cached_strategy_runner(*etag_case(tmp_path, etag_server))
    await run()
    with etag_serving("v2", b"<html><body>changed content</body></html>"):
        second = await run()
    assert second.status == "success"
    assert "changed content" in (second.markdown or "")


async def test_crawl_request_serves_recursive_pages_from_content_cache(tmp_path):
    """Recursive crawl_request reuses the content cache for seed AND discovered pages."""
    from urllib.parse import urlparse

    # ``calls`` is the chokepoint's own URL log: this test is about what the
    # content cache serves, so it needs to see which fetches reached the
    # network, not to build a second site_fetcher to ask.
    fetched_urls: list[str] = []

    pages = {
        "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
        "/a": (200, "<html>a</html>", "text/html"),
    }
    limits = {"max_depth": 1, "max_pages": 5}
    content_cache = ContentCache(tmp_path)

    def page_fetches():
        """Declared pages that reached the network, in order.

        Discovery probes (robots.txt) are answered by the scripted site too,
        and they are not pages -- this is about the content cache serving
        pages, so the filter is "is a declared page", not a literal path list.
        """
        return [p for u in fetched_urls if (p := urlparse(u).path or "/") in pages]

    async def crawl_twice_over():
        await crawled_results(pages, limits=limits, content_cache=content_cache, calls=fetched_urls)

    await crawl_twice_over()
    first = page_fetches()
    await crawl_twice_over()
    second = page_fetches()[len(first) :]  # only the 2nd crawl's page fetches
    assert first == ["/", "/a"], first
    # Both the seed and the discovered child must be served from the content cache.
    assert second == [], f"recursive pages should be served from content cache, re-fetched: {second}"


async def test_mirror_gate_uses_serialized_size_not_shallow_getsizeof(tmp_path):
    """Shallow sys.getsizeof on a dict sees only the container (~200 B), so a
    multi-MB serialized entry passed the old mirror gate and landed in RAM
    (worst case _MEMORY_CACHE_MAX x full-size entries)."""
    cache = ContentCache(tmp_path)
    fat = {"body": "x" * (cache._MEMORY_CACHE_MAX_SIZE + 1024)}
    assert sys.getsizeof(fat) < cache._MEMORY_CACHE_MAX_SIZE  # shallow size: tiny

    await cache.aset("fat-key", fat)
    assert cache._read_envelope("fat-key") is not None  # disk: written
    assert "fat-key" not in cache._memory_cache  # RAM mirror: must skip


async def test_mirror_gate_small_value_still_mirrors(tmp_path):
    cache = ContentCache(tmp_path)
    await cache.aset("small-key", {"body": "tiny"})
    assert "small-key" in cache._memory_cache


async def test_cold_aget_does_not_mirror_oversized_envelope(tmp_path):
    """After a warm restart ``_index_sizes`` is empty for pre-existing files,
    so ``aget`` must gate the RAM mirror on the actual envelope bytes (stat),
    not ``_index_sizes.get(key, 0)`` -- which defaults to 0 and lets a multi-MB
    cold read into the small-value mirror (the RAM exhaustion path the
    serialized-size gate was built to close)."""
    fat = {"body": "x" * (1024 * 1024 + 1024)}  # > _MEMORY_CACHE_MAX_SIZE
    writer = ContentCache(tmp_path)
    await writer.aset("fat-key", fat)
    assert "fat-key" not in writer._memory_cache  # write gate already refuses

    # Fresh instance over the same dir: cold index, pre-existing disk file.
    reader = ContentCache(tmp_path)
    assert reader._index_sizes.get("fat-key") is None  # truly cold
    got = await reader.aget("fat-key")
    assert got is not None  # served from disk
    assert "fat-key" not in reader._memory_cache  # must NOT land in RAM mirror


async def test_cold_aget_still_mirrors_small_envelope(tmp_path):
    """Parity: a genuinely small cold read keeps the RAM-mirror benefit."""
    writer = ContentCache(tmp_path)
    await writer.aset("small-key", {"body": "tiny"})
    reader = ContentCache(tmp_path)
    assert reader._index_sizes.get("small-key") is None
    got = await reader.aget("small-key")
    assert got == {"body": "tiny"}
    assert "small-key" in reader._memory_cache  # small: mirror is correct


class TestContentCacheOverwrite:
    def test_content_cache_set_overwrite(self):
        with scoped_cache(default_ttl=60) as cache:
            cache.set("key", {"data": "first"})
            cache.set("key", {"data": "second"})
            assert cache.get("key") == {"data": "second"}


class TestContentCacheCorruptEntry:
    def test_content_cache_get_corrupt_json(self):
        with scoped_cache(default_ttl=60) as cache:
            cache._path_for("testkey").write_text("{broken json")
            assert cache.get("testkey") is None

    def test_content_cache_prune_corrupt_entry(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key1")
        # The corrupt file carries the name prune would compute, so a prune that
        # cannot read it must skip it rather than count it as removed.
        cache._path_for("badkey").write_text("not valid json")
        clock[0] = 1002.0  # advance past the TTL
        assert cache.prune() == 1  # only key1 is expired and removable

    def test_content_cache_prune_os_error(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key1")
        clock[0] = 1002.0
        with entry_replaced_by_directory(cache, "key1"):
            cache.prune()

    def test_content_cache_clear_os_error(self):
        with scoped_cache(default_ttl=60) as cache:
            cache.set("key1", {"data": "1"})
            with entry_replaced_by_directory(cache, "key1"):
                cache.clear()


class TestSharedContentCache:
    def test_engine_shared_content_cache(self):
        from app.crawl.content_cache import ContentCache
        from app.crawl.engine import _get_shared_content_cache

        cache = _get_shared_content_cache()
        assert isinstance(cache, ContentCache)


def test_default_max_entries_is_bounded_not_none():
    """ContentCache has a bounded default max_entries."""

    with scoped_cache(default_ttl=3600) as c:
        # The default should be a positive integer, not None (which meant unbounded).
        assert c.max_entries is not None
        assert isinstance(c.max_entries, int)
        assert c.max_entries > 0


def test_max_entries_enforces_bound(tmp_path):
    """Cache never exceeds max_entries."""
    cache = _make_cache(tmp_path, max_entries=3)
    cache.set("a", 1)
    cache.set("b", 2)
    cache.set("c", 3)
    # 3 entries
    assert cache.get("a") == 1
    assert cache.get("b") == 2
    assert cache.get("c") == 3

    # Add 'd' -> one must be evicted to stay at 3
    cache.set("d", 4)
    # Total cached entries should be 3
    assert len(list(cache.cache_dir.iterdir())) == 3
    # All remaining gets should work or return None (evicted)
    # At least one of a,b,c was evicted; d is present
    assert cache.get("d") == 4
    # Verify we didn't lose all old entries (at least 2 of a,b,c remain)
    remaining = sum(1 for k in ("a", "b", "c") if cache.get(k) is not None)
    assert remaining >= 2


def test_explicit_max_entries_zero_disables_limit():
    """Max_entries=0 means no limit (opt-out).

    Two records, run twice: once under a cap of one, once under a cap of zero.
    The contrast at the same volume is the point -- "nothing was evicted" only
    says something next to a cache that did evict, and pinning both here means
    the second half cannot pass by being broken too.

    The volume is two because the claim is that the count cap is not applied.
    The cap reads ``max_entries > 0 and len(index) > max_entries``, so a zero
    with anything in the index evicts every entry if the ``> 0`` test is ever
    dropped -- two records catch that, one would not. It used to write 15,000,
    which took longer than every other test in this file combined and proved
    only that a large number of writes finish.
    """
    with scoped_cache(default_ttl=3600, max_entries=1) as bounded:
        bounded.set("k0", 0)
        bounded.set("k1", 1)
        assert bounded.get("k0") is None, "a cap of one should not have kept two records"

    with scoped_cache(default_ttl=3600, max_entries=0) as uncapped:
        uncapped.set("k0", 0)
        uncapped.set("k1", 1)
        assert uncapped.get("k0") == 0
        assert uncapped.get("k1") == 1


def test_lru_keeps_recently_used_entries():
    """Accessing an entry protects it from next eviction."""

    with scoped_cache(default_ttl=3600, max_entries=3) as c:
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


def reopen_cache(directory, **settings) -> ContentCache:
    """A fresh instance over a directory a previous instance wrote.

    It deliberately seeds nothing: what the sites that use it are after is the
    state a cold process finds on disk, and writing an entry first would warm
    the very mirror they are testing.
    """
    return ContentCache(directory, **settings)


def _make_cache(tmp_path, **kwargs):
    kwargs.setdefault("default_ttl", 60)
    return ContentCache(str(tmp_path), **kwargs)


async def _seeded_then_write_fails(cache, monkeypatch, body="old"):
    """A cache holding a persisted value, armed to fail the next write.

    Both rollback tests start from the same place: a live entry that an
    in-flight write fails against, so what is under test is what the *failed*
    write left behind. Spelling that start state in each test meant a change to
    the seeded value or to how a write is broken had to be made twice, and one
    copy could be left asserting about a state the other never created.
    """
    await cache.aset("k", {"body": body}, ttl=60)
    watch_writes(cache, fail="new")


def budgeted_cache(tmp_path, *, max_total_bytes: int = 400, **settings) -> ContentCache:
    """A cache whose total byte budget is the only limit in play.

    The entry cap and the count cap are lifted out of the way, so a test about
    byte accounting cannot pass because the count cap evicted first.
    """
    return _make_cache(
        tmp_path,
        max_entries=10_000,
        max_entry_bytes=10_000,
        max_total_bytes=max_total_bytes,
        **settings,
    )


def _at_cap_budget() -> tuple[float, dict, int]:
    """``(now, value, cap)`` where ``cap`` is exactly the on-disk envelope size.

    The budget is the *envelope's* byte count, not the value's -- the cap that
    admits an entry has to be derived from what actually lands on disk, so the
    two are computed together here and a change to either moves both.
    """
    now = 1000.0
    value = {"body": "x" * 150}
    envelope = {"expires": now + 60, "value": value, "cached_at": now}
    cap = len(json.dumps(envelope))
    assert cap > len(json.dumps(value))
    return now, value, cap


class TestContentCacheByteBudget:
    def test_oversized_entry_is_refused(self, tmp_path):
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        cache.set("big", {"body": "x" * 10_000}, ttl=60)
        assert cache.get("big") is None
        assert list(cache.cache_dir.iterdir()) == []

    def test_entry_at_cap_is_accepted_with_envelope_accounting(self, tmp_path):
        now, value, cap = _at_cap_budget()
        cache = _make_cache(tmp_path, max_entry_bytes=cap, clock=lambda: now)
        cache.set("at-cap", value, ttl=60)

        assert cache.get("at-cap") == value
        assert cache._path_for("at-cap").stat().st_size == cap

    async def test_async_entry_at_cap_is_accepted_with_envelope_accounting(self, tmp_path):
        now, value, cap = _at_cap_budget()
        cache = _make_cache(tmp_path / "async", max_entry_bytes=cap, clock=lambda: now)

        await cache.aset("at-cap", value, ttl=60)

        assert await cache.aget("at-cap") == value
        assert cache._path_for("at-cap").stat().st_size == cap

    def test_total_budget_evicts_lru_first(self, tmp_path):
        cache = budgeted_cache(tmp_path)
        cache.set("k1", {"body": "a" * 100}, ttl=60)
        cache.set("k2", {"body": "b" * 100}, ttl=60)
        assert cache.get("k1") is not None
        assert cache.get("k2") is not None
        # Third write overflows the budget: k1 (least recently used) goes.
        cache.set("k3", {"body": "c" * 100}, ttl=60)
        assert cache.get("k1") is None
        assert cache.get("k2") is not None
        assert cache.get("k3") is not None
        on_disk = sum(p.stat().st_size for p in cache.cache_dir.iterdir())
        assert on_disk <= 400

    async def test_aset_enforces_budget(self, tmp_path):
        cache = budgeted_cache(tmp_path)
        await cache.aset("k1", {"body": "a" * 100}, ttl=60)
        await cache.aset("k2", {"body": "b" * 100}, ttl=60)
        await cache.aset("k3", {"body": "c" * 100}, ttl=60)
        assert await cache.aget("k1") is None
        assert await cache.aget("k3") is not None

    async def test_aset_oversized_never_written(self, tmp_path, monkeypatch):
        """The async path must refuse oversized entries before any disk
        write -- the payload must not transiently hit disk (crash window).
        Mirrors set(): pre-serialize, size-check under the lock, then write.
        The spy sits on ``_write_data``, the seam the real code uses."""
        cache = _make_cache(tmp_path, max_entry_bytes=200)
        written: list[str] = []

        watch_writes(cache, calls=written)
        await cache.aset("big", {"body": "x" * 10_000}, ttl=60)
        assert written == []
        assert list(cache.cache_dir.iterdir()) == []
        assert await cache.aget("big") is None
        # A small write in the same cache still works.
        await cache.aset("ok", {"body": "y"}, ttl=60)
        assert await cache.aget("ok") is not None

    async def test_aset_accounting_matches_written_bytes(self, tmp_path):
        """Aset() byte accounting must equal the exact bytes on disk
        (cross-instance equality is nondeterministic: the envelope embeds
        clock() floats whose repr width varies between writes)."""
        cache_b = _make_cache(tmp_path / "b", max_entry_bytes=10_000)
        await cache_b.aset("same", {"body": "z" * 500}, ttl=60)
        path = cache_b._index["same"]
        assert cache_b._index_sizes["same"] == path.stat().st_size
        assert cache_b._index_total_bytes == path.stat().st_size

    async def test_aset_write_failure_leaves_no_dangling_index(self, tmp_path, monkeypatch):
        """A failed disk write must roll the index entry back so aget()
        does not keep consulting a path that does not exist."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index
        assert cache._index_sizes.get("k", 0) == 0
        assert await cache.aget("k") is None

    def test_prune_repairs_unknown_sizes(self, tmp_path):
        """Prune() must reconcile byte accounting for index entries whose
        recorded size is missing (e.g. crash between index insert and
        accounting update) by stat'ing the surviving file."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("k", {"body": "v" * 300}, ttl=60)
        # Simulate crash residue: index entry survives, its recorded size
        # and the total were lost.
        cache._index_sizes.clear()
        cache._index_total_bytes = 0
        assert cache.prune() == 0
        path = cache._index["k"]
        assert cache._index_sizes["k"] == path.stat().st_size
        assert cache._index_total_bytes == path.stat().st_size
        assert json.loads(path.read_text())["value"] == {"body": "v" * 300}

    def test_set_routes_through_write_seam(self, tmp_path, monkeypatch):
        """Twins: the sync write must go through _write_data like aset, so
        a failure is observable/testable at the same seam (a spy on an inline
        atomic_write_text call site is silently vacuous)."""
        cache = _make_cache(tmp_path)
        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            cache.set("k", {"body": "v"}, ttl=60)
        assert "k" not in cache._index

    async def test_aget_cold_reads_honor_mirror_count_cap(self, tmp_path):
        """Twins: aget's disk-hit mirror enforces the same
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
        """Twins: a hot URL read through the SYNC path warms the RAM
        mirror exactly like the async path (the accelerator contract is
        method-independent)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        cache.set("hot", {"body": "small"}, ttl=60)
        cache._memory_cache.clear()
        assert cache.get("hot") == {"body": "small"}
        assert "hot" in cache._memory_cache

    async def test_aset_write_failure_rolls_back_stale_mirror(self, tmp_path, monkeypatch):
        """Twins: when the async disk write fails with a surviving older
        entry, the in-memory mirror must not keep serving the value that was
        never persisted — rollback covers RAM, not just the index."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await _seeded_then_write_fails(cache, monkeypatch)
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new"}, ttl=60)
        assert await cache.aget("k") == {"body": "old"}
        assert cache.get("k") == {"body": "old"}

    async def test_aset_new_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """A failed write of a NEW key must not move _index_total_bytes.

        Bytes enter the total only at commit (after a successful write), so the
        old rollback's `total -= size` decremented bytes that were never added;
        the max(0,...) clamp only hid it when the cache was otherwise empty."
        """
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await cache.aset("keep", {"body": "v" * 200}, ttl=60)  # counts once, stays
        total_before = cache._index_total_bytes
        assert total_before == sum(cache._index_sizes.values()) > 0

        watch_writes(cache, fail=True)
        with pytest.raises(OSError):
            await cache.aset("ghost", {"body": "g" * 300}, ttl=60)
        assert "ghost" not in cache._index
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())

    async def test_aset_existing_key_write_failure_preserves_byte_total(self, tmp_path, monkeypatch):
        """An existing key whose re-write fails keeps its prior bytes counted
        (the old rollback applied a spurious `prior_size - size` delta)."""
        cache = _make_cache(tmp_path, max_entry_bytes=10_000)
        await _seeded_then_write_fails(cache, monkeypatch)
        total_before = cache._index_total_bytes
        assert cache._index_sizes["k"] > 0
        watch_writes(cache, fail="new")
        with pytest.raises(OSError):
            await cache.aset("k", {"body": "new" * 100}, ttl=60)
        assert cache._index_total_bytes == total_before
        assert cache._index_total_bytes == sum(cache._index_sizes.values())
        assert await cache.aget("k") == {"body": "old"}


class TestExpiredEntryFreesByteAccounting:
    """Reading an expired (or unreadable) entry popped ``_index`` but left
    ``_index_sizes``/``_index_total_bytes`` untouched. The phantom bytes keep
    ``_over_limits()`` true, so the next ``set()``'s ``_enforce_limits`` evicts
    LIVE LRU entries it should not -- a sustained self-inflicted cache wipe-out
    on a TTL-busy cache.
    """

    def test_expired_read_frees_byte_accounting(self, tmp_path):
        cache, now = make_expiring_cache(tmp_path / "cache")
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
        cache, now = make_expiring_cache(tmp_path / "cache")
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

    def test_content_cache_get_expired(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1)
        clock[0] = 1002.0
        assert cache.get("key") is None

    @pytest.mark.parametrize("drop", ["prune", "clear"])
    def test_content_cache_drops_entries_and_reports_the_count(self, tmp_path, drop):
        """Whatever retired them, the entries are gone and the count said so.

        ``prune`` retires on TTL and ``clear`` retires unconditionally; the
        property under test is identical, so it is one test over the two
        operations. Advancing the clock for both costs nothing and keeps the
        fixture to one shape -- spelled per operation it was two copies of the
        same four assertions, which is how the ``clear`` copy ended up not
        checking that the count matched the entries that disappeared.
        """
        cache, clock = make_seeded_cache(tmp_path, "key1", "key2", default_ttl=1)
        clock[0] = 1002.0
        assert getattr(cache, drop)() == 2
        assert cache.get("key1") is None
        assert cache.get("key2") is None

    async def test_aset_aget_roundtrip(self):
        """Async disk cache stores and retrieves, including a large (HTML-sized)
        value that hits the on-disk path."""
        with scoped_cache(default_ttl=60) as cache:
            big = "<html>" + ("x" * 2_000_000) + "</html>"
            await cache.aset("k1", big)
            # RAM mirror only keeps <1MiB, so this exercises the disk read path.
            assert await cache.aget("k1") == big

    async def test_aset_writes_off_event_loop(self):
        """The disk write (json.dumps of a large value) runs in a worker thread."""
        with scoped_cache(default_ttl=60) as cache:
            seen: dict[str, Any] = {}
            watch_writes(cache, on_write=lambda path, data: seen.update(thread=threading.current_thread()))
            await cache.aset("k2", "y" * 2_000_000)
        assert seen, "write never happened"
        assert seen["thread"] is not threading.current_thread()


class TestCachedAtReadsMetadataWithoutDeserializingTheValue:
    """Cached_at_of must not deserialize the whole envelope on every call.

    Sitemap freshness checks call this for many keys per crawl, and each
    envelope can hold a multi-MB body. Reading the value bytes off disk (and
    JSON-parsing them) to answer a timestamp question makes freshness O(bytes)
    instead of O(keys). The timestamp is now answered from a metadata mirror
    that is warmed on set and on the first read, with _read_envelope remaining
    the single chokepoint for full reads and for every state mutation.
    """

    def test_cached_at_after_set_reads_no_envelope(self, tmp_path):
        cache, _ = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"big": "x" * 100})
        cache.set("k", {"big": "y" * 100})  # ensure the mirror is refreshed

        with watching_envelope_reads(cache) as reads:
            assert cache.cached_at_of("k") is not None
            assert cache.cached_at_of("k") is not None
        assert reads == [], f"freshness check deserialized the envelope: {reads}"

    def test_cached_at_value_matches_the_envelope(self, tmp_path):
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"data": "value"})
        assert cache.cached_at_of("k") == clock[0]

    def test_cached_at_of_a_cold_on_disk_entry_answers_and_then_warms(self, tmp_path):
        """A warm restart (fresh instance, same directory) still answers
        correctly from disk, and the read pays for itself on later calls."""
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=60, value={"data": "value"})
        cached_at = cache.cached_at_of("k")

        # The restarted instance reads the same time base, or the entry it finds
        # is expired before it is ever looked at.
        reopened = reopen_cache(tmp_path, default_ttl=60, clock=lambda: clock[0])
        with watching_envelope_reads(reopened) as reads:
            # Cold mirror: one full read is unavoidable and correct.
            assert reopened.cached_at_of("k") == cached_at
            # Now warm: later freshness checks never touch the envelope.
            assert reopened.cached_at_of("k") == cached_at
        assert len(reads) == 1, f"expected one cold read, got {reads}"

    def test_expired_entry_reports_none_through_the_chokepoint(self, tmp_path):
        """An expired mirror entry must still be expired and dropped by the
        shared chokepoint, not silently answered from stale metadata."""
        cache, clock = make_seeded_cache(tmp_path, "k", default_ttl=10, value={"data": "value"})
        assert cache.cached_at_of("k") is not None
        clock[0] += 1000.0
        assert cache.cached_at_of("k") is None
        # The expired entry is gone from the index, not just unanswered.
        assert "k" not in cache._index

    def test_evicted_entry_is_not_answered_from_stale_metadata(self, tmp_path):
        with scoped_cache(default_ttl=60, clock=lambda: 1000.0, max_entries=1) as cache:
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
        cache = cc.ContentCache(tmp_path, max_entries=2, max_total_bytes=None, default_ttl=0)
        with watching_entry_unlinks(monkeypatch, cache) as held:
            # Over-cap by one: the oldest entry is evicted and its file unlinked.
            cache.set(cache.make_key("a"), {"v": 1})
            cache.set(cache.make_key("b"), {"v": 2})
            cache.set(cache.make_key("c"), {"v": 3})

        assert held, "the eviction unlinked nothing"
        assert not any(held), "unlink ran while holding the cache lock"
        assert cache.get(cache.make_key("a")) is None
        assert cache.get(cache.make_key("c")) == {"v": 3}

    def test_expiry_unlinks_outside_the_lock(self, monkeypatch, tmp_path):
        """The TTL-expiry path in _read_envelope unlinks outside the lock too."""
        cache, clock = make_seeded_cache(tmp_path, "key", default_ttl=1, value={"data": "value"})
        with watching_entry_unlinks(monkeypatch, cache) as held:
            clock[0] = 1002.0
            assert cache.get("key") is None
        assert held, "the expiry unlinked nothing"
        assert not any(held), "expiry unlink ran while holding the cache lock"

    def test_re_set_key_survives_its_stale_eviction_unlink(self, tmp_path):
        """A key re-set after its eviction reuses the same deterministic path.

        The deferred unlink must spare a file that has become live again,
        otherwise the fresh write is destroyed and only a self-healing cache
        miss hides the loss.
        """
        cache = cc.ContentCache(tmp_path, max_entries=1, max_total_bytes=None, default_ttl=0)
        key = cache.make_key("hot")
        cache.set(key, {"v": 1})

        # Evict exactly as _enforce_limits does, but leave the unlink pending
        # so the re-registration can land inside the deferral window.
        doomed: list[tuple[str | None, Path]] = []
        with cache._lock:
            cache._drop_locked(key, doomed)

        # The key is written again to the same deterministic path.
        cache.set(key, {"v": "fresh"})
        cache._unlink_doomed(doomed)

        assert cache.get(key) == {"v": "fresh"}
