"""Robots.txt non-200 status classes: two verdicts, and the source that says which.

401/403/429 are an application-layer refusal -- decline (AGENTS.md forbids continuing an
explicit denial); other 4xx are "unavailable" (RFC 9309). The value says which."""

from __future__ import annotations

import pytest

from crawlutron.models.host_policy import HostPolicy
from crawlutron.transport.robots import (
    ROBOTS_FAILURE_TTL_SECONDS,
    RobotsRules,
    load_robots_rules,
    robots_cache,
)
from crawlutron.vocabulary.robots import RobotsSource
from tests._fetch_doubles import FetcherDouble
from tests._policy_factories import canonical

_ROBOTS_URL = "https://waf.test/robots.txt"


def _decode_queue(*answers: tuple[str | None, int | None]):
    """A decode double answering each call with the next (body, status)."""
    queue = iter(answers)
    calls: list[str] = []

    async def decode(fetcher, url, headers, timeout_seconds):
        assert url.raw.endswith("/robots.txt"), f"fetched {url.raw}"
        calls.append(url.raw)
        return next(queue)

    return decode, calls


async def _load(origin: str, *answers):
    decode, calls = _decode_queue(*answers)
    # The decode double never touches the fetcher; the double satisfies the
    # HttpFetcher contract without a live transport.
    rules = await load_robots_rules(
        canonical(origin),
        FetcherDouble(lambda *_: (None, None)),
        5,
        fetch_and_decode=decode,
        policy=HostPolicy.unrestricted(),
    )
    return rules, calls


def _loads_against(decode, policy):
    """A load bound to one decode double and policy, for tests re-using them."""

    def _load():
        return load_robots_rules(
            canonical("https://waf.test"),
            FetcherDouble(lambda *_: (None, None)),
            5,
            fetch_and_decode=decode,
            policy=policy,
        )

    return _load


UNAVAILABLE_STATUSES = [400, 404, 410, 451]


@pytest.mark.parametrize("status", UNAVAILABLE_STATUSES)
async def test_unavailable_status_4xx_allows_crawling(status):
    """RFC 9309 2.3.1.3: a plain 4xx means robots.txt is unavailable, so the
    crawler MAY access any resources on the server (empty rule set)."""
    rules, _ = await _load("https://waf.test", (None, status))
    assert rules.rules == [], status
    assert rules.source is RobotsSource.unavailable, status


@pytest.mark.parametrize("status", [401, 403, 429])
async def test_application_layer_refusal_denies(status):
    """401/403/429 are an explicit application-layer denial, not an unavailable
    robots.txt -- fail closed (complete disallow), and say it was a refusal."""
    rules, _ = await _load("https://waf.test", (None, status))
    assert rules.rules == [(False, "/")], status
    assert rules.source is RobotsSource.refused, status


async def test_robots_403_refusal_is_fail_closed_on_the_short_clock():
    """The rework ruling: 403 is an application-layer refusal -- deny, but the
    entry still expires on the short clock so a later 200 is honored."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        rules, _ = await _load("https://waf.test", (None, 403))
        assert rules.rules == [(False, "/")]
        delta = robots_cache.time_until_expiry(("https://waf.test", policy))
        assert delta is not None and delta <= ROBOTS_FAILURE_TTL_SECONDS
    finally:
        robots_cache.clear()


async def test_refusal_status_is_not_cached_as_a_denial():
    """One transient refusal (403 here) must not outlive the failure TTL: the
    entry expires on the short clock, so a later 200 is honored on the next load."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        decode, calls = _decode_queue((None, 403))
        load_with = _loads_against(decode, policy)

        # The short-lived entry still serves the window without re-fetching.
        again = await load_with()
        assert again.rules == [(False, "/")]
        assert len(calls) == 1
    finally:
        robots_cache.clear()


async def test_later_robots_200_wins_after_unavailable_status():
    """After the short TTL lapses, a successful robots fetch is parsed and its
    rules enforced -- a 403 never becomes a standing decision."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        await _load("https://waf.test", (None, 403))
        # Lapse the short-lived unavailable entry.
        robots_cache.put(
            ("https://waf.test", policy),
            RobotsRules([], [], None, RobotsSource.unavailable),
            ttl=-1.0,
        )
        decode, calls = _decode_queue(("User-agent: *\nDisallow: /private\n", 200))
        load_with = _loads_against(decode, policy)

        rules = await load_with()
        assert rules.rules == [(False, "/private")]
        assert rules.source is RobotsSource.policy
        assert len(calls) == 1
    finally:
        robots_cache.clear()


async def test_robots_5xx_still_fails_closed():
    """RFC 9309 2.3.1.4: 5xx means unreachable -- complete disallow."""
    rules, _ = await _load("https://down.test", (None, 500))
    assert rules.rules == [(False, "/")]
    assert rules.source is RobotsSource.unreachable


async def test_robots_5xx_refusal_ttl_is_the_short_failure_ttl():
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        decode, _ = _decode_queue((None, 500))
        await load_robots_rules(
            canonical("https://down.test"),
            FetcherDouble(lambda *_: (None, None)),
            5,
            fetch_and_decode=decode,
            policy=policy,
        )
        delta = robots_cache.time_until_expiry(("https://down.test", policy))
        assert delta is not None and delta <= ROBOTS_FAILURE_TTL_SECONDS
    finally:
        robots_cache.clear()


async def test_robots_404_is_unavailable_too_on_the_short_clock():
    """404 was already allow-all, but on the long TTL -- a site whose robots
    appears later must not wait 600s for the empty verdict to lapse."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        await _load("https://gone.test", (None, 404))
        delta = robots_cache.time_until_expiry(("https://gone.test", policy))
        assert delta is not None and delta <= ROBOTS_FAILURE_TTL_SECONDS
    finally:
        robots_cache.clear()


async def test_robots_200_keeps_the_long_ttl():
    """RFC 9309 2.3.1.1: a successful fetch is a real decision -- long TTL."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        await _load("https://gone.test", ("User-agent: *\nDisallow: /x\n", 200))
        delta = robots_cache.time_until_expiry(("https://gone.test", policy))
        assert delta is not None and delta > ROBOTS_FAILURE_TTL_SECONDS
    finally:
        robots_cache.clear()
