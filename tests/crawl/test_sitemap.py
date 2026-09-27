"""Sitemap discovery: seed batches, last-modified freshness, conditional fetch and fanout bounds."""

import asyncio
from datetime import UTC, datetime, timedelta

from _assertions import install_to_thread_spy
from _fetcher_factories import (
    concurrent_fetcher,
    make_fetch,
    request_fetcher,
    routed_fetcher,
    scripted_fetcher,
)
from _policy_factories import UNRESTRICTED_HOST_POLICY, crawl_policy

from app.crawl.content_cache import ContentCache
from app.crawl.engine import _parse_lastmod, _seed_is_fresh, _stale_seeds
from app.crawl.fetch_helpers import _SITEMAP_CACHE, fetch_sitemap_urls
from app.crawl.strategies.content_cache_key import _content_cache_key
from app.crawl.strategies.request_profile import _headers_for_strategy
from app.map import build_site_map


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


async def test_304_reuses_cached_urls_without_reparsing():
    url = "http://x.test/sitemap.xml"
    # Seed an EXPIRED cache entry (forces a re-fetch attempt).
    # Seed an EXPIRED entry (negative ttl) through the chokepoint seam.
    _SITEMAP_CACHE.put(
        (url, UNRESTRICTED_HOST_POLICY),
        ([("http://x.test/p1", None)], "v1", None),
        ttl=-1.0,
    )
    calls = []

    def respond(u, headers):
        if headers.get("If-None-Match") == "v1":
            return _resp(304, etag="v1")  # not modified
        return _resp(200, "<x></x>", etag="v1")

    fetcher = request_fetcher(respond, calls=calls)

    out = await fetch_sitemap_urls(
        fetcher,
        url,
        10,
        conditional={"If-None-Match": "v1"},
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    assert out == {("http://x.test/p1", None)}
    # Conditional header was sent.
    assert any(c.get("If-None-Match") == "v1" for c in calls)
    # Cache TTL was refreshed (entry still present, not re-parsed).
    assert (
        _SITEMAP_CACHE.peek((url, UNRESTRICTED_HOST_POLICY)) is not None
    )  # TTL refreshed, entry fresh again


async def test_etag_captured_and_stored_on_200():
    url = "http://x.test/sitemap.xml"
    _SITEMAP_CACHE.discard((url, UNRESTRICTED_HOST_POLICY))
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
    assert ("http://x.test/p1", "2024-01-01") in out
    # The new etag/last-modified are stored so the next crawl can send a 304 check.
    assert _SITEMAP_CACHE.peek((url, UNRESTRICTED_HOST_POLICY))[1] == "v2"
    assert _SITEMAP_CACHE.peek((url, UNRESTRICTED_HOST_POLICY))[2] == "Wed, 01 Jan 2024 00:00:00 GMT"


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
        url = "http://fanout.test/sitemap.xml"
        _SITEMAP_CACHE.discard((url, UNRESTRICTED_HOST_POLICY))
        index_body = _index_xml(200)
        child_fetches = 0
        tasks_created = 0
        real_create_task = asyncio.create_task

        def counting_create_task(coro, **kwargs):
            nonlocal tasks_created
            tasks_created += 1
            return real_create_task(coro, **kwargs)

        monkeypatch.setattr(asyncio, "create_task", counting_create_task)

        # A scheduling point, not a duration: what this test measures is how
        # many child fetches the fan-out is willing to have in flight at once,
        # and one loop yield is enough for every admitted child to be inside
        # that window. The counter is the harness's; the routing is the test's.
        def respond(u, headers):
            nonlocal child_fetches
            if u == url:
                return _resp_test_sitemap_fanout(index_body)
            child_fetches += 1
            return _resp_test_sitemap_fanout(_child_xml(int(u.split("child")[1].split(".")[0])))

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
            _SITEMAP_CACHE.discard((url, UNRESTRICTED_HOST_POLICY))
        assert len(out) == 20
        # Enqueue-time cap: never schedule more children than still needed.
        assert child_fetches <= 20
        assert tasks_created <= 20
        # Bounded concurrency: chunks, not one task per child, as the
        # harness counted the overlap rather than this test restating it.
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
        "http://x.test/sitemap.xml",
        10,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    # returns (loc, lastmod) tuples
    by_loc = {loc: lastmod for loc, lastmod in urls}
    assert by_loc["http://x.test/a"] == "2024-01-15"
    assert by_loc["http://x.test/b"] is None
    assert by_loc["http://x.test/c"] == "2023-06-01T00:00:00Z"


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
        "http://x.test/sitemap.xml",
        10,
        host_policy=UNRESTRICTED_HOST_POLICY,
    )
    assert ("http://x.test/p", "2024-03-03") in urls


def _policy(extra=None):
    p = {"strategies": ["HTTP"], "extraction": "trafilara", "headers": {}}
    if extra:
        p.update(extra)
    return p


def test_parse_lastmod_variants():
    assert _parse_lastmod("2024-01-15") is not None
    assert _parse_lastmod("2024-01-15T10:30:00Z") is not None
    assert _parse_lastmod("Mon, 15 Jan 2024 10:30:00 GMT") is not None
    assert _parse_lastmod("") is None
    assert _parse_lastmod(None) is None
    assert _parse_lastmod("not-a-date") is None


def _cache_with_fresh(tmp_path, url, lastmod, policy):
    """Seed the content cache so ``url`` is fresh as of ``lastmod``.

    The key must match what sitemap seeding looks up, so the caller passes the
    exact policy the engine derives (unit tests use ``_policy_test_sitemap_seed_batch()`` directly; the
    integration test derives it via ``_payload_policy``).
    """
    cache = ContentCache(str(tmp_path), clock=lambda: 1_700_000_000.0)
    strategy = policy.get("strategies") or ["HTTP_DECLARED_CRAWLER"]
    key = _content_cache_key(
        url,
        strategy[0],
        _headers_for_strategy(strategy[0], policy, url),
        policy.get("extraction"),
        policy.get("css_selector"),
        policy.get("xpath"),
    )
    cache.set(key, {"html": "<html>cached</html>"})
    return cache


def test_seed_is_fresh(tmp_path):
    url = "http://example.com/page"
    cache = _cache_with_fresh(tmp_path, url, "2020-01-01T00:00:00Z", _policy())

    # sitemap older than our cache -> cached copy is fresh -> skip
    assert _seed_is_fresh(url, "2020-01-01T00:00:00Z", cache, _policy()) is True
    # sitemap newer than our cache -> stale -> fetch
    assert _seed_is_fresh(url, "2025-01-01T00:00:00Z", cache, _policy()) is False
    # no lastmod -> fetch
    assert _seed_is_fresh(url, None, cache, _policy()) is False
    # not cached at all -> fetch
    assert _seed_is_fresh("http://example.com/other", "2020-01-01T00:00:00Z", cache, _policy()) is False


def test_seed_is_fresh_requires_cache():
    assert _seed_is_fresh("http://example.com/x", "2020-01-01T00:00:00Z", None, _policy()) is False


_CRAWL_URL = "http://seed-batch.test/"


def _policy_test_sitemap_seed_batch():
    return {
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "extraction": "trafilatura",
        "headers": {},
    }


class TestStaleSeedBatch:
    def test_batch_matches_per_url_verdicts(self, tmp_path):
        """The batch helper returns exactly the URLs whose cached copy is
        fresh (the same verdicts the per-URL helper produced before T7)."""
        fresh = "http://seed-batch.test/fresh"
        stale = "http://seed-batch.test/stale"
        cache = _cache_with_fresh(tmp_path, fresh, "2020-01-01T00:00:00Z", _policy_test_sitemap_seed_batch())
        policy = _policy_test_sitemap_seed_batch()

        stale_set = _stale_seeds(
            [(fresh, "2020-01-01T00:00:00Z"), (stale, "2025-01-01T00:00:00Z")],
            cache,
            policy,
        )
        assert stale_set == {fresh}

    def test_unreadable_or_missing_entries_fail_open(self, tmp_path):
        """A cache read error must never cause a seed to be skipped: an
        unreadable/expired entry is treated as NOT fresh so the URL is still
        crawled."""
        cache = _cache_with_fresh(
            tmp_path, "http://seed-batch.test/ok", "2020-01-01", _policy_test_sitemap_seed_batch()
        )
        policy = _policy_test_sitemap_seed_batch()

        stale_set = _stale_seeds(
            [("http://seed-batch.test/never-cached", "2020-01-01"), (None, "2020-01-01")],
            cache,
            policy,
        )
        assert stale_set == set()


class TestSeedingIsOffLoop:
    async def test_freshness_filter_runs_in_a_worker_thread(self, monkeypatch, tmp_path):
        """The engine's sitemap seeding must offload the freshness batch via
        ``asyncio.to_thread`` rather than parsing envelopes on the loop."""
        from app.crawl.engine import crawl_request

        seen = install_to_thread_spy(monkeypatch)
        fresh_url = "http://seed-batch.test/fresh"
        payload = {
            "url": _CRAWL_URL,
            "policy": crawl_policy(discovery={"sitemaps": True}, limits={"max_depth": 1}),
        }

        from app.crawl.payload_helpers import _payload_policy
        from app.settings import CrawlerSettings

        derived = _payload_policy(payload, CrawlerSettings.from_mapping())
        cache = _cache_with_fresh(tmp_path, fresh_url, "2020-01-01T00:00:00Z", derived)
        fetched: list[str] = []

        # The fresh-seed sitemap body is named, not inlined in a three-branch
        # fetcher: what this test is about is which URLs get fetched at all.
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

        assert "_stale_seeds" in seen, "freshness filtering was not offloaded off the loop"
        # The fresh seed was skipped (#3): only robots.txt, the sitemap and the
        # crawled origin were fetched -- never the cached page itself.
        assert fresh_url not in fetched


def _crawl_response(original_url, links=(), *, sitemaps=(), feeds=()):
    """The one result shape ``build_site_map`` reads, from the pieces a test varies.

    Every site-map test is about what the aggregation *does* with the URLs -- dedupe
    them, resolve them against the origin, keep the sitemaps and feeds separate --
    and none is about the envelope around them. Restating the envelope per test
    meant the two URLs each test listed were easy to change without the assertion
    changing with them, which is how a site-map test ends up passing on a link it
    no longer declares.
    """
    return {
        "results": [
            {
                "original_url": original_url,
                "final_url": original_url,
                "diagnostics": {
                    "robots": {"sitemaps": list(sitemaps)},
                    "discovery": {"sitemap_urls": list(sitemaps), "feeds": list(feeds)},
                },
                "discovered_links": [{"url": link, "normalized_url": link} for link in links],
            }
        ]
    }


def test_build_site_map_aggregates_and_dedupes():
    crawl_response = _crawl_response(
        "http://x.example/",
        links=[
            "http://x.example/a",
            "http://x.example/b",
            "http://x.example/a",  # the duplicate this test exists to collapse
        ],
        sitemaps=["http://x.example/sitemap.xml", "http://x.example/sitemap2.xml"],
        feeds=["http://x.example/feed.xml"],
    )
    m = build_site_map(crawl_response, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["url"] == "http://x.example/"
    assert m["links"] == ["http://x.example/a", "http://x.example/b"]
    assert "http://x.example/sitemap.xml" in m["sitemaps"]
    assert "http://x.example/sitemap2.xml" in m["sitemaps"]
    assert m["feeds"] == ["http://x.example/feed.xml"]
    assert m["all_urls"] == [
        "http://x.example/a",
        "http://x.example/b",
        "http://x.example/sitemap.xml",
        "http://x.example/sitemap2.xml",
        "http://x.example/feed.xml",
    ]


def test_build_site_map_handles_dataclass_links():
    from app.models import DiscoveredLink

    link = DiscoveredLink(
        url="https://r/page",
        normalized_url="https://r/page",
        text="",
        rel=[],
        source_url="https://r/",
        host="r",
        is_same_host=True,
    )
    crawl_response = {"results": [{"final_url": "http://r/", "discovered_links": [link], "diagnostics": {}}]}
    m = build_site_map(crawl_response, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["links"] == ["https://r/page"]
    assert m["all_urls"] == ["https://r/page"]


def test_build_site_map_empty():
    m = build_site_map({"results": []}, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["url"] is None
    assert m["all_urls"] == []


def test_build_site_map_resolves_relative_paths():
    # L6: origin-relative paths must resolve against the root instead of
    # leaking verbatim -- the SiteMapResponse contract promises normalized
    # public URLs.
    crawl_response = _crawl_response(
        "http://x.example/dir/",
        links=["/page1", "http://x.example/abs"],
    )
    m = build_site_map(crawl_response, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["links"] == ["http://x.example/abs", "http://x.example/page1"]
    assert all(not u.startswith("/") for u in m["all_urls"])


# ---------------------------------------------------------------------------
# H7: future-skew bound on sitemap <lastmod> (reject garbage at the parse
# chokepoint; degradation direction is re-fetch, never "fresher than now").
# ---------------------------------------------------------------------------


def _iso_offset(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def test_parse_lastmod_accepts_past_and_plausible_future():
    assert _parse_lastmod(_iso_offset(-3600)) is not None
    # Clock skew is a fact of life: a future stamp within the bound is trusted.
    assert _parse_lastmod(_iso_offset(600)) is not None


def test_parse_lastmod_rejects_far_future(monkeypatch):
    """A >1-day-future stamp is a garbage claim, not freshness information."""
    assert _parse_lastmod(_iso_offset(86400 * 2)) is None


def test_future_skew_bound_is_configurable(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", "0")
    assert _parse_lastmod(_iso_offset(300)) is None
    assert _parse_lastmod(_iso_offset(-300)) is not None


def test_negative_bound_config_falls_back_to_floor(monkeypatch):
    """A negative skew would reject stamps from the last N seconds (the
    site's clock runs ahead); the lo=0 floor keeps 'trust the past' intact."""
    monkeypatch.setenv("BROWSER_CRAWLER_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", "-3600")
    assert _parse_lastmod(_iso_offset(-1800)) is not None


def test_far_future_lastmod_never_skips_a_cached_seed():
    """Contract pin: garbage lastmod fails open to re-fetch, and cheaply.

    With the bound the parse returns None, so the freshness check declines
    before composing cache keys -- the seed enters the frontier without the
    per-strategy disk reads a far-future timestamp used to pay for a verdict
    it could never return.
    """

    class _Cache:
        def cached_at_of(self, key):
            raise AssertionError("far-future lastmod must short-circuit before any disk read")

    assert _seed_is_fresh("http://x.test/p", _iso_offset(86400 * 30), _Cache(), {}) is False
