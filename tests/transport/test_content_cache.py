"""Content cache: TTL, disk persistence, memory mirror, byte budget and unlink discipline."""

from __future__ import annotations

import contextlib
import sys
from collections.abc import Callable, Generator

import pytest

import crawlutron.transport.content_cache as cc
from crawlutron.models.json_value import JsonValue
from crawlutron.transport.cache import HttpCache
from crawlutron.transport.content_cache import ContentCache
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._cache_fixtures import _make_cache, counting_entry_reads, on_disk_bytes, reopen_cache, scoped_cache
from tests._fetch_doubles import FetcherDouble
from tests._fetcher_factories import clocked_cache, crawled_results, run_http_strategy
from tests._lock_scope_walks import calls_inside_self_lock
from tests._policy_factories import canonical
from tests.helpers.server import EtagHandler


def etag_case(tmp_path, etag_server):
    """``(url, policy, fetcher, cache, content_cache)`` for the etag server's URL.

    The real fetcher against the real local server, with a tap over it.
    Only the wiring is test-owned; nothing here answers HTTP.
    """
    port = etag_server.server_address[1]
    return (
        f"http://127.0.0.1:{port}/",
        {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": "full", "headers": {}},
        FetcherDouble(
            lambda url, sent_headers, timeout, cap: default_http_fetcher(url, sent_headers, timeout, cap)
        ),
        HttpCache(),
        ContentCache(tmp_path),
    )


def cached_strategy_runner(url, policy, fetcher, cache=None, content_cache=None):
    """The HTTP strategy over one URL and the two caches, handed back unrun.

    "what the *second* crawl did" is the subject, so the arrangement is owned
    once and the moment of each run is the test's; neither cache = uncached.
    """

    async def run():
        return await run_http_strategy(url, policy, fetcher, cache=cache, content_cache=content_cache)

    return run


@contextlib.contextmanager
def etag_serving(etag: str, body: bytes) -> Generator[None, None, None]:
    """Make the etag server answer with a new validator and body, then restore.

    The restore values are read off the handler, not re-spelled -- a copied
    default drifts invisibly the moment the fixture body changes.
    """
    original = (EtagHandler.etag, EtagHandler.body)
    EtagHandler.etag, EtagHandler.body = etag, body
    try:
        yield
    finally:
        EtagHandler.etag, EtagHandler.body = original


def test_read_paths_refuse_the_dead_ttl_argument():
    """The read paths' ``ttl`` kwarg was dead (never read) and is gone: passing it
    is a loud TypeError, not a silently-ignored knob (#222)."""
    with scoped_cache(default_ttl=3600) as c:
        c.set("k", "v")
        for method in (c.get, c.cached_at_of, c.aget):
            # The dead kwarg is a loud TypeError; an object callable is the
            # honest spelling of "deliberately wrong call shape".
            bad_call: Callable[..., object] = method
            with pytest.raises(TypeError):
                bad_call("k", ttl=0)


def test_set_get_roundtrip():
    with scoped_cache(default_ttl=3600) as c:
        assert c.get("missing") is None
        c.set("k", {"a": 1, "b": "x"})
        assert c.get("k") == {"a": 1, "b": "x"}


def test_set_writes_disk_outside_the_lock():
    """``set`` performs the disk write outside the commit lock, like ``aset``.

    The write seam must not sit inside ``with self._lock:``: one slow disk
    write would block every other cache operation. Asserted on the live
    source so the two set twins cannot drift apart again."""
    offenders = calls_inside_self_lock("crawlutron/transport/content_cache.py", ("set",), ("write_data",))
    assert offenders == [], (
        "set() calls write_data inside the commit lock: one slow disk write "
        "blocks every other cache operation (the aset twin runs it outside)"
    )


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


def test_zero_ttl_stores_nothing(tmp_path):
    """Convention (#394): ttl 0 or less means OFF -- the entry is never stored,
    never served. 'Never expires' is not expressible through a TTL."""
    c, clock = clocked_cache(tmp_path / "z", default_ttl=0, clock_start=0.0)
    c.set("k", "v")
    assert c.get("k") is None
    c.set("k", "v", ttl=0)
    assert c.get("k") is None
    c.set("k", "v", ttl=-5)
    assert c.get("k") is None
    assert on_disk_bytes(c) == 0


def test_prune_removes_only_expired(tmp_path):
    c, clock = clocked_cache(tmp_path / "p", default_ttl=10, clock_start=0.0)
    c.set("a", 1)
    c.set("b", 2)
    clock[0] = 20.0  # a,b expired (expire at 10)
    c.set("c", 3)  # expires at 30, still fresh at t=20
    assert c.prune() == 2
    assert c.get("c") == 3
    assert c.get("a") is None


def test_prune_sweeps_orphaned_tmp_files(tmp_path):
    """A SIGKILL-mid-write leaves a ``.tmp`` orphan the cache glob never matches
    and the byte budget never counts; prune must sweep it (#241)."""
    import os

    from tests._clocks import arrange_clock

    c = _make_cache(tmp_path / "orphans")
    c.set("k", "v")
    state, _ = arrange_clock()
    orphan = c.cache_dir / f".{c.namespace}.stale.{state['now']}.json.tmp"
    orphan.write_bytes(b"partial\n")
    old = state["now"] - 7200
    os.utime(orphan, (old, old))

    c.prune()

    assert not orphan.exists(), "prune must sweep an orphaned .tmp file (#241)"


def test_get_serves_a_disk_hit_after_the_ram_mirror_goes_cold(tmp_path):
    """A cold RAM mirror does not lose a value: get() falls through to disk.

    The disk-hit read path returns the envelope even when nothing is mirrored,
    which the oversized (never-mirrored) value under the gate forces.
    """
    c = ContentCache(cache_dir=tmp_path / "m", default_ttl=3600)
    big = "x" * (2 * 1024 * 1024)  # past the mirror size gate: cold in RAM
    c.set("big", big)

    assert c.get("big") == big


# --------------------------- integration: real server, no mocks ---------------------------


def real_fetcher(calls: list[str] | None = None):
    """The production HTTP fetcher, optionally under a tap that records URLs.

    Every integration below is about what the *real* transport did, so the
    fetcher is production's own; only the observation is test-owned.
    """
    return (
        default_http_fetcher
        if calls is None
        else FetcherDouble(
            lambda url, sent_headers, timeout, cap: default_http_fetcher(url, sent_headers, timeout, cap),
            calls,
        )
    )


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
    assert fetched == [canonical(f"{base_url}/").raw]  # the cache prevented the second real fetch


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

    # ``calls`` is the chokepoint's own URL log: this test is about what the content cache serves, so it needs
    # to see which fetches reached the network, not to build a second site_fetcher to ask.
    fetched_urls: list[str] = []

    pages = {
        "/": (200, '<html><body><a href="/a">a</a></body></html>', "text/html"),
        "/a": (200, "<html>a</html>", "text/html"),
    }
    limits: dict[str, JsonValue] = {"max_depth": 1, "max_pages": 5}
    content_cache = ContentCache(tmp_path)

    def page_fetches():
        """Declared pages that reached the network, in order.

        Discovery probes are answered by the scripted site too and are not
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


async def test_mirror_gate_uses_serialized_size_not_shallow_getsizeof(tmp_path, monkeypatch):
    """Shallow sys.getsizeof on a dict sees only the container (~200 B), so a
    multi-MB serialized entry passed the old mirror gate and landed in RAM
    (worst case MEMORY_CACHE_MAX x full-size entries)."""
    cache = ContentCache(tmp_path)
    fat: dict[str, JsonValue] = {"body": "x" * (cc.MEMORY_CACHE_MAX_SIZE + 1024)}
    assert sys.getsizeof(fat) < cc.MEMORY_CACHE_MAX_SIZE  # shallow size: tiny

    await cache.aset("fat-key", fat)
    assert await cache.aget("fat-key") is not None  # disk: written
    # Not mirrored: each aget falls through to a fresh disk read.
    with counting_entry_reads(monkeypatch, cache, "fat-key") as reads:
        assert await cache.aget("fat-key") is not None
        assert reads == [1]


async def test_mirror_gate_small_value_still_mirrors(tmp_path, monkeypatch):
    cache = ContentCache(tmp_path)
    await cache.aset("small-key", {"body": "tiny"})
    # Mirrored: a RAM hit reads no bytes.
    with counting_entry_reads(monkeypatch, cache, "small-key") as reads:
        assert await cache.aget("small-key") == {"body": "tiny"}
        assert reads == [0]


async def test_cold_aget_does_not_mirror_oversized_envelope(tmp_path, monkeypatch):
    """After a warm restart the index is cold for pre-existing files: ``aget``
    must gate the mirror on the actual bytes, not a zero default, so a multi-MB
    read never lands in RAM."""
    fat: dict[str, JsonValue] = {"body": "x" * (1024 * 1024 + 1024)}  # > MEMORY_CACHE_MAX_SIZE
    writer = ContentCache(tmp_path)
    await writer.aset("fat-key", fat)

    # Fresh instance over the same dir: cold index, pre-existing disk file.
    reader = ContentCache(tmp_path)
    got = await reader.aget("fat-key")
    assert got is not None  # served from disk
    # Still not mirrored by the cold read: the next aget is one disk read.
    with counting_entry_reads(monkeypatch, reader, "fat-key") as reads:
        assert await reader.aget("fat-key") is not None
        assert reads == [1]


async def test_cold_aget_still_mirrors_small_envelope(tmp_path, monkeypatch):
    """Parity: a genuinely small cold read keeps the RAM-mirror benefit."""
    writer = ContentCache(tmp_path)
    await writer.aset("small-key", {"body": "tiny"})
    reader = ContentCache(tmp_path)
    got = await reader.aget("small-key")
    assert got == {"body": "tiny"}
    # The cold read warmed the mirror: a later aget is served from RAM.
    with counting_entry_reads(monkeypatch, reader, "small-key") as reads:
        assert await reader.aget("small-key") == {"body": "tiny"}
        assert reads == [0]


async def _directive_runner(url: str, headers: dict[str, str], tmp_path):
    """A cached strategy runner over a response carrying ``headers``, counting fetches."""
    fetches: dict[str, int] = {"n": 0}

    def respond(url, _sent):
        fetches["n"] += 1
        return {"status": 200, "headers": headers, "body": b"<html>body</html>"}

    run = cached_strategy_runner(
        url,
        {"strategies": [HTTP_DECLARED_CRAWLER], "extraction": "full", "headers": {}},
        FetcherDouble(lambda url, sent_headers, *_: respond(url, sent_headers)),
        content_cache=ContentCache(tmp_path),
    )
    return run, fetches


async def test_no_store_response_not_persisted(tmp_path):
    """A ``cache-control: no-store`` body must never be written to the disk content
    cache: a second crawl re-fetches it, never serving the single-use body (pins #247)."""
    run, fetches = await _directive_runner(
        "http://example.com/secret", {"cache-control": "no-store", "etag": '"once"'}, tmp_path
    )
    await run()
    await run()
    # no-store: the second crawl must hit the network again, not serve a cached copy.
    assert fetches["n"] == 2, fetches


async def test_no_cache_response_revalidated_on_replay(tmp_path):
    """A ``cache-control: no-cache`` body with no validator must not replay stale:
    on the second crawl it is revalidated (a full re-fetch here, since no validator
    exists to send) rather than served from the disk cache (pins #247)."""
    run, fetches = await _directive_runner(
        "http://example.com/must-revalidate", {"cache-control": "no-cache"}, tmp_path
    )
    await run()
    await run()
    # no-cache with no validator: the second crawl must not serve the stored body.
    assert fetches["n"] == 2, fetches


async def test_private_response_is_never_stored_for_replay(tmp_path):
    """RFC 9111 5.2.2.6: ``private`` marks single-user content; a shared cache stores nothing.

    Two callers (different cookie namespaces) share the key when neither sends a
    Cookie yet, so only forbidding the store keeps B from being served A's body.
    """
    from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE

    run, fetches = await _directive_runner(
        "http://example.com/private", {"cache-control": "private"}, tmp_path
    )
    token = COOKIE_NAMESPACE.set("tenant-a")
    try:
        await run()
    finally:
        COOKIE_NAMESPACE.reset(token)
    token = COOKIE_NAMESPACE.set("tenant-b")
    try:
        await run()
    finally:
        COOKIE_NAMESPACE.reset(token)
    # Caller B must re-fetch; replaying A's private body is the leak.
    assert fetches["n"] == 2, fetches


async def test_max_age_bounds_the_replay_window(tmp_path):
    """RFC 9111 5.2.2.1: freshness comes from the origin's max-age, not the local
    default TTL -- a max-age=0 response must be stale immediately, so the second
    crawl re-fetches instead of replaying the stored body (pins #385)."""
    run, fetches = await _directive_runner(
        "http://example.com/zero", {"cache-control": "max-age=0"}, tmp_path
    )
    await run()
    await run()
    assert fetches["n"] == 2, fetches


_PAST_EXPIRES = "Sun, 22 Aug 1982 00:00:00 GMT"
_FAR_FUTURE_EXPIRES = "Sun, 22 Aug 2032 00:00:00 GMT"


async def test_expires_in_the_past_is_never_served_from_the_cache(tmp_path):
    """RFC 9111 5.3 (#392): a response carrying only a past Expires is already
    stale -- the second crawl must re-fetch, never replay the stored body."""
    run, fetches = await _directive_runner("http://example.com/stale", {"expires": _PAST_EXPIRES}, tmp_path)
    await run()
    await run()
    assert fetches["n"] == 2, fetches


async def test_future_expires_bounds_the_replay_window(tmp_path):
    """RFC 9111 5.3 (#392): a response with only a future Expires is fresh until
    that instant -- the second crawl replays from the cache (with the local
    default TTL also bounding it, the entry does not outlive its envelope)."""
    run, fetches = await _directive_runner(
        "http://example.com/fresh", {"expires": _FAR_FUTURE_EXPIRES}, tmp_path
    )
    await run()
    await run()
    assert fetches["n"] == 1, fetches


async def test_max_age_beats_a_conflicting_expires(tmp_path):
    """RFC 9111 5.2.2.1 / 5.3 (#392): when both are present, max-age wins --
    a max-age=0 must re-fetch even though Expires claims far-future freshness."""
    run, fetches = await _directive_runner(
        "http://example.com/conflict",
        {"cache-control": "max-age=0", "expires": _FAR_FUTURE_EXPIRES},
        tmp_path,
    )
    await run()
    await run()
    assert fetches["n"] == 2, fetches
