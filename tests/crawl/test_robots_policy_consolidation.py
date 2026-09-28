from __future__ import annotations

import asyncio

import pytest
from _fetcher_factories import make_html_fetcher
from _policy_factories import crawl_policy

from app.api_models import CrawlPolicy
from app.crawl.admission import RobotsGate
from app.crawl.engine import crawl_request
from app.crawl.robots import (
    ROBOTS_CACHE_TTL_SECONDS,
    ROBOTS_FAILURE_TTL_SECONDS,
    load_robots_rules,
    robots_cache,
)
from app.validation import HostPolicy


def _policy(*, respect_robots: bool = True) -> dict:
    return crawl_policy(limits={"respect_robots": respect_robots})


def _gate() -> RobotsGate:
    """A gate over default settings and no host policy.

    The consolidation tests all read one policy through two very different
    paths, so the gate is built once here: the three arguments were restated at
    every call site, and a test that changed one of them stopped being about
    the robots cache at all.
    """
    return RobotsGate(CrawlPolicy(), HostPolicy.unrestricted())


async def test_job_fetch_populates_cache_seen_by_engine():
    robots_cache.clear()
    gate = _gate()
    fetch_calls = 0

    async def decode(fetcher, url, headers, timeout_seconds):
        nonlocal fetch_calls
        fetch_calls += 1
        return (
            "User-agent: *\nDisallow: /private\nCrawl-delay: 2\nSitemap: https://shared.test/sitemap.xml\n",
            200,
        )

    import app.crawl.admission as admission

    original = admission._fetch_and_decode
    admission._fetch_and_decode = decode
    try:
        assert await gate.allows("https://shared.test/public") is True
    finally:
        admission._fetch_and_decode = original

    page_calls: list[str] = []
    result = await crawl_request(
        {"url": "https://shared.test/public", "policy": _policy()},
        http_fetcher=make_html_fetcher(page_calls),
    )
    assert result["results"][0]["status"] == "success"
    assert fetch_calls == 1
    assert robots_cache.get(("https://shared.test", HostPolicy.unrestricted())) == (
        [(False, "/private")],
        ["https://shared.test/sitemap.xml"],
        2.0,
    )
    assert page_calls and all("/robots.txt" not in url for url in page_calls)


async def test_engine_fetch_populates_cache_seen_by_job_path():
    robots_cache.clear()
    engine_calls: list[str] = []
    result = await crawl_request(
        {"url": "https://reverse.test/public", "policy": _policy()},
        http_fetcher=make_html_fetcher(engine_calls),
    )
    assert result["results"][0]["status"] == "success"
    assert sum(url.endswith("/robots.txt") for url in engine_calls) == 1

    gate = _gate()

    async def unexpected_decode(*args):
        raise AssertionError("job path fetched a cached robots policy")

    import app.crawl.admission as admission

    original = admission._fetch_and_decode
    admission._fetch_and_decode = unexpected_decode
    try:
        assert await gate.allows("https://reverse.test/public") is True
    finally:
        admission._fetch_and_decode = original


async def test_engine_and_job_single_flight_each_collapse_same_origin_misses(monkeypatch):
    robots_cache.clear()
    calls = 0

    async def decode(*args):
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return "User-agent: *\nDisallow:\n", 200

    import app.crawl.engine as engine

    monkeypatch.setattr(engine, "_fetch_and_decode", decode)
    await asyncio.gather(
        *(
            crawl_request(
                {"url": f"https://flight.test/page-{index}", "policy": _policy()},
                http_fetcher=make_html_fetcher([]),
            )
            for index in range(5)
        )
    )
    assert calls == 1

    robots_cache.clear()
    calls = 0
    gate = _gate()
    import app.crawl.admission as admission

    monkeypatch.setattr(admission, "_fetch_and_decode", decode)
    assert (
        await asyncio.gather(*(gate.allows(f"https://flight.test/job-{index}") for index in range(5)))
        == [True] * 5
    )
    assert calls == 1


async def test_owner_preserves_404_and_5xx_ttl_semantics(monkeypatch):
    import time

    import app.crawl.robots as owner

    robots_cache.clear()
    statuses = iter((404, 500))
    calls = 0

    async def decode(*args):
        nonlocal calls
        calls += 1
        return None, next(statuses)

    monkeypatch.setattr(owner, "_fetch_and_decode", decode)
    policy = HostPolicy.unrestricted()
    missing = await load_robots_rules("https://missing.test", object(), 5, policy=policy)
    missing_again = await load_robots_rules(
        "https://missing.test",
        object(),
        5,
        policy=policy,
    )
    failure = await load_robots_rules("https://failure.test", object(), 5, policy=policy)
    failure_again = await load_robots_rules(
        "https://failure.test",
        object(),
        5,
        policy=policy,
    )
    assert missing == ([], [], None)
    assert missing_again == ([], [], None)
    assert failure == ([(False, "/")], [], None)
    assert failure_again == ([(False, "/")], [], None)
    assert calls == 2
    missing_expires_at, _missing_value = owner.robots_cache._entries[("https://missing.test", policy)]
    assert missing_expires_at - time.monotonic() == pytest.approx(ROBOTS_CACHE_TTL_SECONDS, abs=5)
    expires_at, _value = owner.robots_cache._entries[("https://failure.test", policy)]
    assert expires_at - time.monotonic() == pytest.approx(ROBOTS_FAILURE_TTL_SECONDS, abs=5)


async def test_respect_false_does_not_fetch_robots_even_when_discovery_requests_it():
    robots_cache.clear()
    calls: list[str] = []
    result = await crawl_request(
        {
            "url": "https://no-robots.test/page",
            "policy": {
                **_policy(respect_robots=False),
                "discovery": {"robots": True},
            },
        },
        http_fetcher=make_html_fetcher(calls),
    )
    assert result["results"][0]["status"] == "success"
    robots_diagnostics = result["results"][0]["diagnostics"]["robots"]
    assert robots_diagnostics["evaluation"] == "disabled"
    assert robots_diagnostics["reused_from_engine"] is False
    assert robots_diagnostics["sitemaps"] == []
    assert not any(url.endswith("/robots.txt") for url in calls)
