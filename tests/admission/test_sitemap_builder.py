from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from crawlutron.admission.policy import read_policy
from crawlutron.api.site_map import build_site_map
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.orchestration.engine import parse_lastmod, seed_is_fresh
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._result_builders import _link, engine_result


def _crawl_response(
    original_url: str,
    links: Sequence[str] = (),
    *,
    sitemaps: Sequence[str] = (),
    feeds: Sequence[str] = (),
) -> ProducerEnvelope:
    """The one result shape ``build_site_map`` reads, from the pieces a test
    varies; every site-map test is about the URLs, not the envelope around
    them."""
    return {
        "request_id": "sitemap-test",
        "submitted_count": 1,
        "policy_schema_version": "test",
        "results": [
            engine_result(
                original_url,
                "rendered",
                final_url=original_url,
                diagnostics={
                    # The sitemap gate reads only these blocks' URL lists; the
                    # remaining RobotsDiagnostics keys are not consumed here.
                    "robots": {"allowed": True, "evaluation": "allowed", "sitemaps": list(sitemaps)},
                    "discovery": {"sitemap_urls": list(sitemaps), "feeds": list(feeds)},
                },
                discovered_links=tuple(_link(link, original_url) for link in links),
            )
        ],
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
    # canonical(): the root's admitted raw, not the submission text.
    assert m["url"] == canonical("http://x.example/").raw
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
    from crawlutron.models.discovered_link import DiscoveredLink

    link = DiscoveredLink(
        url="https://r/page",
        normalized_url=canonical("https://r/page"),
        text="",
        rel=[],
        source_url=canonical("https://r/"),
        host="r",
        is_same_host=True,
    )
    crawl_response: ProducerEnvelope = {
        "request_id": "sitemap-test",
        "submitted_count": 1,
        "policy_schema_version": "test",
        "results": [
            engine_result(
                "http://r/",
                "rendered",
                final_url="http://r/",
                discovered_links=(link,),
            )
        ],
    }
    m = build_site_map(crawl_response, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["links"] == ["https://r/page"]
    assert m["all_urls"] == ["https://r/page"]


def test_build_site_map_empty():
    empty: ProducerEnvelope = {
        "request_id": "sitemap-test",
        "submitted_count": 0,
        "policy_schema_version": "test",
        "results": [],
    }
    m = build_site_map(empty, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["url"] is None
    assert m["all_urls"] == []


def test_build_site_map_resolves_relative_paths():
    crawl_response = _crawl_response(
        "http://x.example/dir/",
        links=["/page1", "http://x.example/abs"],
    )
    m = build_site_map(crawl_response, host_policy=UNRESTRICTED_HOST_POLICY)
    assert m["links"] == ["http://x.example/abs", "http://x.example/page1"]
    assert all(not u.startswith("/") for u in m["all_urls"])


# --------------------------------------------------------------------------- future-skew bound on sitemap


def _iso_offset(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


def test_parse_lastmod_accepts_past_and_plausible_future():
    assert parse_lastmod(_iso_offset(-3600)) is not None
    # Clock skew is a fact of life: a future stamp within the bound is trusted.
    assert parse_lastmod(_iso_offset(600)) is not None


def test_parse_lastmod_rejects_far_future(monkeypatch):
    """A >1-day-future stamp is a garbage claim, not freshness information."""
    assert parse_lastmod(_iso_offset(86400 * 2)) is None


def test_future_skew_bound_is_configurable(monkeypatch):
    monkeypatch.setenv("CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", "0")
    assert parse_lastmod(_iso_offset(300)) is None
    assert parse_lastmod(_iso_offset(-300)) is not None


def test_negative_bound_config_falls_back_to_floor(monkeypatch):
    """A negative skew would reject stamps from the last N seconds (the
    site's clock runs ahead); the lo=0 floor keeps 'trust the past' intact."""
    monkeypatch.setenv("CRAWLUTRON_SITEMAP_LASTMOD_FUTURE_SKEW_SECONDS", "-3600")
    assert parse_lastmod(_iso_offset(-1800)) is not None


def test_far_future_lastmod_never_skips_a_cached_seed():
    """Contract pin: garbage lastmod fails open to re-fetch, and cheaply.

    The freshness check declines before composing cache keys, so a far-future
    timestamp never pays for the per-strategy disk reads."""

    class _Cache:
        def cached_at_of(self, key):
            raise AssertionError("far-future lastmod must short-circuit before any disk read")

    assert (
        seed_is_fresh(canonical("http://x.test/p"), _iso_offset(86400 * 30), _Cache(), read_policy({}))
        is False
    )
