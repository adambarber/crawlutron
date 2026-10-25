"""Sitemap discovery: seed batches, last-modified freshness, conditional fetch and fanout bounds."""

import asyncio
from functools import partial

from app.admission.policy import read_policy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.engine import parse_lastmod, seed_is_fresh, stale_seeds
from app.orchestration.fetch_helpers import SITEMAP_CACHE, fetch_sitemap_urls
from app.transport.content_cache import ContentCache
from app.transport.content_cache_key import build_content_cache_key
from app.transport.request_profile import headers_for_strategy
from app.vocabulary.limits import SITEMAP_CACHE_TTL_SECONDS, SITEMAP_FAILURE_TTL_SECONDS
from tests._assertions import install_to_thread_spy
from tests._fetcher_factories import (
    concurrent_fetcher,
    make_fetch,
    request_fetcher,
    routed_fetcher,
    scripted_fetcher,
)
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical, crawl_policy


def _resp(status, body="", etag=None, last_modified=None, ctype="application/xml"):
    headers = {"content-type": ctype}
    if etag:
        headers["etag"] = etag
    if last_modified:
        headers["last-modified"] = last_modified
    return make_fetch(
        url="http://x.test/sitemap.xml",
        final_url="http://x.test/sitemap.xml",
        status=status,
        headers=headers,
        body=body.encode("utf-8"),
    )


def _seed_expired_entry(url: NormalizedUrl, *, etag: str = "v1") -> None:
    """An expired cached sitemap entry, seeded through the chokepoint seam."""
    SITEMAP_CACHE.put(
        (url.raw, UNRESTRICTED_HOST_POLICY),
        ([("http://x.test/p1", None)], etag, None),
        ttl=-1.0,
    )


def _replay_recording_fetcher(*, fresh_etag: str = "v2"):
    """A fetcher that records headers and answers 304 on a replayed validator.

    A replay that never happened gets a fresh body with ``fresh_etag`` instead."""
    sent: list[dict[str, str]] = []

    def respond(u, headers):
        if headers.get("If-None-Match") == "v1":
            return _resp(304, etag="v1")
        return _resp(200, "<x></x>", etag=fresh_etag)

    return request_fetcher(respond, record_headers=sent), sent


async def test_expired_entry_replays_its_validator_without_the_caller_asking():
    """The expiry replay: the fetcher alone decides whether to re-send them."""
    url = canonical("http://x.test/sitemap.xml")
    _seed_expired_entry(url)
    fetcher, sent = _replay_recording_fetcher()

    out = await fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY)

    # The prior etag rode on the request, the origin answered 304, and the cached set was reused -- which only
    # happens if the replay fired.
    assert any(h.get("If-None-Match") == "v1" for h in sent), sent
    assert out == {("http://x.test/p1", None)}


async def test_304_reuses_cached_urls_without_reparsing():
    url = canonical("http://x.test/sitemap.xml")
    _seed_expired_entry(url)
    fetcher, sent = _replay_recording_fetcher(fresh_etag="v1")

    out = await fetch_sitemap_urls(
        fetcher,
        url,
        10,
        conditional={"If-None-Match": "v1"},
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    assert out == {("http://x.test/p1", None)}
    # Conditional header was sent.
    assert any(h.get("If-None-Match") == "v1" for h in sent)
    # Cache TTL was refreshed (entry still present, not re-parsed).
    assert (
        SITEMAP_CACHE.peek((url.raw, UNRESTRICTED_HOST_POLICY)) is not None
    )  # TTL refreshed, entry fresh again


async def test_etag_captured_and_stored_on_200():
    url = canonical("http://x.test/sitemap.xml")
    SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/p1</loc><lastmod>2024-01-01</lastmod></url>
    </urlset>"""

    fetcher = scripted_fetcher(
        status=200,
        headers={
            "content-type": "application/xml",
            "etag": "v2",
            "last-modified": "Wed, 01 Jan 2024 00:00:00 GMT",
        },
        body=xml.encode("utf-8"),
    )

    out = await fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY)
    assert (canonical("http://x.test/p1"), "2024-01-01") in out
    # The new etag/last-modified are stored so the next crawl can send a 304 check.
    cached = SITEMAP_CACHE.peek((url.raw, UNRESTRICTED_HOST_POLICY))
    assert cached is not None, "the fetch must have stored its conditional-request headers"
    assert cached[1] == "v2"
    assert cached[2] == "Wed, 01 Jan 2024 00:00:00 GMT"


def _resp_test_sitemap_fanout(body: str):
    return make_fetch(
        url="http://fanout.test/sitemap.xml",
        final_url="http://fanout.test/sitemap.xml",
        status=200,
        ctype="application/xml",
        body=body.encode("utf-8"),
    )


def _index_xml(n: int) -> str:
    children = "".join(f"<sitemap><loc>http://fanout.test/child{i}.xml</loc></sitemap>" for i in range(n))
    return (
        '<?xml version="1.0"?>'
        '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{children}</sitemapindex>"
    )


def _child_xml(i: int) -> str:
    return (
        '<?xml version="1.0"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>http://fanout.test/p{i}</loc></url></urlset>"
    )


class TestSitemapFanoutBound:
    async def test_child_scheduling_capped_by_remaining_need(self, monkeypatch):
        url = canonical("http://fanout.test/sitemap.xml")
        SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
        index_body = _index_xml(200)
        child_fetches = 0
        tasks_created = 0
        real_create_task = asyncio.create_task

        def counting_create_task(coro, **kwargs):
            # Only child walks count: the #498 single-flight wrapper is one
            # bookkeeping task per flight, not part of the fan-out cap.
            nonlocal tasks_created
            if getattr(coro, "__qualname__", "").startswith("_recurse_sitemap"):
                tasks_created += 1
            return real_create_task(coro, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", counting_create_task)

        def respond(u, headers):
            nonlocal child_fetches
            if u.raw == url.raw:
                return _resp_test_sitemap_fanout(index_body)
            child_fetches += 1
            return _resp_test_sitemap_fanout(_child_xml(int(u.raw.split("child")[1].split(".")[0])))

        fetcher = concurrent_fetcher(respond)

        try:
            out = await fetch_sitemap_urls(
                fetcher,
                url,
                10,
                max_urls=20,
                host_policy=UNRESTRICTED_HOST_POLICY,
            )
        finally:
            SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
        assert len(out) == 20
        # Enqueue-time cap: never schedule more children than still needed.
        assert child_fetches <= 20
        assert tasks_created <= 20
        # Bounded concurrency: chunks, not one task per child, as the harness counted the overlap rather than
        # this test restating it.
        assert fetcher.peak() <= 16


def _sitemap_fetcher(xml: str):
    return scripted_fetcher(status=200, ctype="application/xml", body=xml.encode("utf-8"))


async def test_extracts_lastmod_per_loc():
    xml = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/a</loc><lastmod>2024-01-15</lastmod></url>
      <url><loc>http://x.test/b</loc></url>
      <url><loc>http://x.test/c</loc><lastmod>2023-06-01T00:00:00Z</lastmod></url>
    </urlset>"""
    urls = await fetch_sitemap_urls(
        _sitemap_fetcher(xml),
        canonical("http://x.test/sitemap.xml"),
        10,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    # Returns (loc, lastmod) tuples with admitted loc values
    by_loc = {loc: lastmod for loc, lastmod in urls}
    assert by_loc[canonical("http://x.test/a")] == "2024-01-15"
    assert by_loc[canonical("http://x.test/b")] is None
    assert by_loc[canonical("http://x.test/c")] == "2023-06-01T00:00:00Z"


async def test_index_children_recurse_without_lastmod():
    index = """<?xml version="1.0"?>
    <sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>http://x.test/sub.xml</loc></sitemap>
    </sitemapindex>"""
    sub = """<?xml version="1.0"?>
    <urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>http://x.test/p</loc><lastmod>2024-03-03</lastmod></url>
    </urlset>"""

    fetcher = routed_fetcher(
        {"/sitemap.xml": (200, index, "application/xml"), "sub.xml": (200, sub, "application/xml")},
        missing="raise",
    )

    urls = await fetch_sitemap_urls(
        fetcher,
        canonical("http://x.test/sitemap.xml"),
        10,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    assert (canonical("http://x.test/p"), "2024-03-03") in urls


async def test_failed_sitemap_fetch_is_cached_on_the_short_clock():
    """A transient failure must not poison discovery for the full hour:
    the failure entry lapses on the short clock like robots' does."""
    url = canonical("http://x.test/sitemap.xml")
    SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
    try:
        fetcher = scripted_fetcher(status=500, ctype="application/xml", body=b"")
        out = await fetch_sitemap_urls(fetcher, url, 10, host_policy=UNRESTRICTED_HOST_POLICY)
        assert out == set()
        delta = SITEMAP_CACHE.time_until_expiry((url.raw, UNRESTRICTED_HOST_POLICY))
        assert delta is not None and delta <= SITEMAP_FAILURE_TTL_SECONDS
    finally:
        SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))


async def test_successful_sitemap_fetch_keeps_the_long_ttl():
    """A resolved sitemap is a real result -- the full TTL, not the failure clock."""
    url = canonical("http://x.test/sitemap.xml")
    SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))
    try:
        out = await fetch_sitemap_urls(
            _sitemap_fetcher(
                '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
                "<url><loc>http://x.test/p</loc></url></urlset>"
            ),
            url,
            10,
            host_policy=UNRESTRICTED_HOST_POLICY,
        )
        assert out
        delta = SITEMAP_CACHE.time_until_expiry((url.raw, UNRESTRICTED_HOST_POLICY))
        assert delta is not None and delta > SITEMAP_FAILURE_TTL_SECONDS
        assert delta <= SITEMAP_CACHE_TTL_SECONDS
    finally:
        SITEMAP_CACHE.discard((url.raw, UNRESTRICTED_HOST_POLICY))


def _policy(extra=None, *, extraction=None):
    """The sitemap suite's policy, as a validated model.

    Seeding caches key off model fields, so one validated spelling serves
    both the key builder and the engine."""
    base = {"extraction": extraction} if extraction else {}
    return read_policy(crawl_policy(["HTTP_DECLARED_CRAWLER"], **{**base, **(extra or {})}))


def test_parse_lastmod_variants():
    assert parse_lastmod("2024-01-15") is not None
    assert parse_lastmod("2024-01-15T10:30:00Z") is not None
    assert parse_lastmod("Mon, 15 Jan 2024 10:30:00 GMT") is not None
    assert parse_lastmod("") is None
    assert parse_lastmod(None) is None
    assert parse_lastmod("not-a-date") is None


def _cache_with_fresh(tmp_path, url, lastmod, policy):
    """Seed the content cache so ``url`` is fresh as of ``lastmod``.

    The key must match what sitemap seeding looks up, so the caller passes
    the exact policy the engine derives."""
    cache = ContentCache(str(tmp_path), clock=lambda: 1_700_000_000.0)
    strategy = list(policy.strategies) or ["HTTP_DECLARED_CRAWLER"]
    target = canonical(url)
    key = build_content_cache_key(
        target,
        strategy[0],
        headers_for_strategy(strategy[0], read_policy(policy), target),
        policy.extraction,
        policy.css_selector,
        policy.xpath,
    )
    cache.set(key, {"html": "<html>cached</html>"})
    return cache


def test_seed_is_fresh(tmp_path):
    url = canonical("http://example.com/page")
    cache = _cache_with_fresh(tmp_path, url.raw, "2020-01-01T00:00:00Z", _policy())

    # Sitemap older than our cache -> cached copy is fresh -> skip
    assert seed_is_fresh(url, "2020-01-01T00:00:00Z", cache, _policy()) is True
    # Sitemap newer than our cache -> stale -> fetch
    assert seed_is_fresh(url, "2025-01-01T00:00:00Z", cache, _policy()) is False
    # No lastmod -> fetch
    assert seed_is_fresh(url, None, cache, _policy()) is False
    # Not cached at all -> fetch
    not_cached = canonical("http://example.com/other")
    assert seed_is_fresh(not_cached, "2020-01-01T00:00:00Z", cache, _policy()) is False


def test_seed_is_fresh_requires_cache():
    assert seed_is_fresh(canonical("http://example.com/x"), "2020-01-01T00:00:00Z", None, _policy()) is False


_CRAWL_URL = "http://seed-batch.test/"


# The sitemap seed-batch policy: same chain, HTML main-text extraction only.
_POLICY_SEED_BATCH = partial(_policy, extraction="trafilatura")


class TestStaleSeedBatch:
    def test_batch_matches_per_url_verdicts(self, tmp_path):
        """The batch helper returns exactly the URLs whose cached copy is
        fresh (the same verdicts the per-URL helper produced before )."""
        fresh = "http://seed-batch.test/fresh"
        stale = "http://seed-batch.test/stale"
        cache = _cache_with_fresh(tmp_path, fresh, "2020-01-01T00:00:00Z", _POLICY_SEED_BATCH())
        policy = _POLICY_SEED_BATCH()

        stale_set = stale_seeds(
            [(canonical(fresh), "2020-01-01T00:00:00Z"), (canonical(stale), "2025-01-01T00:00:00Z")],
            cache,
            read_policy(policy),
        )
        assert {u.raw for u in stale_set} == {fresh}

    def test_unreadable_or_missing_entries_fail_open(self, tmp_path):
        """A cache read error must never cause a seed to be skipped: an
        unreadable/expired entry is treated as NOT fresh so the URL is still
        crawled."""
        cache = _cache_with_fresh(tmp_path, "http://seed-batch.test/ok", "2020-01-01", _POLICY_SEED_BATCH())
        policy = _POLICY_SEED_BATCH()

        # An unadmittable loc ("" here) never reaches this helper any more: the resolve chokepoint drops it at
        # extraction, reported as skipped. Fail-open is still proven by the never-cached candidate below.
        stale_set = stale_seeds(
            [(canonical("http://seed-batch.test/never-cached"), "2020-01-01")],
            cache,
            read_policy(policy),
        )
        assert stale_set == set()


class TestSeedingIsOffLoop:
    async def test_freshness_filter_runs_in_a_worker_thread(self, monkeypatch, tmp_path):
        """The engine's sitemap seeding must offload the freshness batch via
        ``asyncio.to_thread`` rather than parsing envelopes on the loop."""
        from app.orchestration.engine import crawl_request

        seen = install_to_thread_spy(monkeypatch)
        fresh_url = "http://seed-batch.test/fresh"
        payload = {
            "url": _CRAWL_URL,
            "policy": crawl_policy(discovery={"sitemaps": True}, limits={"max_depth": 1}),
        }

        from app.admission.payload_helpers import payload_policy
        from app.admission.policy import PolicyTier

        derived = payload_policy(payload, PolicyTier.model_defaults())
        cache = _cache_with_fresh(tmp_path, fresh_url, "2020-01-01T00:00:00Z", derived)
        fetched: list[str] = []

        # The fresh-seed sitemap body is named, not inlined in a three-branch fetcher: what this test is about
        # is which URLs get fetched at all.
        fresh_sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            f"<url><loc>{fresh_url}</loc><lastmod>2020-01-01T00:00:00Z</lastmod></url>"
            "</urlset>"
        )
        fetcher = routed_fetcher(
            {
                "/robots.txt": (200, "User-agent: *\nAllow: /\n", "text/plain"),
                "sitemap": (200, fresh_sitemap, "application/xml"),
            },
            calls=fetched,
            default=(200, b"<html><body>page</body></html>", "text/html"),
        )

        await crawl_request(payload, http_fetcher=fetcher, content_cache=cache)

        assert "stale_seeds" in seen, "freshness filtering was not offloaded off the loop"
        # The fresh seed was skipped: only robots.txt, the sitemap and the crawled origin were fetched --
        # never the cached page itself.
        assert fresh_url not in fetched
