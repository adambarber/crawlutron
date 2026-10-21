"""RFC 9309 2.3.1.3: robots.txt 4xx statuses mean "unavailable", not "disallow-all".

The crawler MAY crawl; the refusal is never cached as a standing decision
(a later 200 wins), and 5xx stays fail-closed per 2.3.1.4.
"""

from __future__ import annotations

import pytest

from app.models.host_policy import HostPolicy
from app.orchestration.robots import (
    ROBOTS_FAILURE_TTL_SECONDS,
    load_robots_rules,
    robots_cache,
)
from tests._policy_factories import canonical

_ROBOTS_URL = "https://waf.test/robots.txt"


def _decode_queue(*answers):
    """A decode double answering each call with the next (body, status)."""
    answers = iter(answers)
    calls: list[str] = []

    async def decode(fetcher, url, headers, timeout_seconds):
        assert url.raw.endswith("/robots.txt"), f"fetched {url.raw}"
        calls.append(url.raw)
        return next(answers)

    return decode, calls


async def _load(origin: str, *answers):
    decode, calls = _decode_queue(*answers)
    rules = await load_robots_rules(
        canonical(origin), object(), 5, fetch_and_decode=decode, policy=HostPolicy.unrestricted()
    )
    return rules, calls


@pytest.mark.parametrize("status", [401, 403, 410, 429])
async def test_unavailable_status_4xx_allows_crawling(status):
    """RFC 9309 2.3.1.3: any 4xx means robots.txt is unavailable, so the
    crawler MAY access any resources on the server (empty rule set)."""
    rules, _ = await _load("https://waf.test", (None, status))
    assert rules == ([], [], None), status


async def test_robots_403_response_is_not_treated_as_disallow_all():
    """The defect: 403 fell into the disallow-all branch and blocked the site."""
    rules, _ = await _load("https://waf.test", (None, 403))
    assert rules != ([(False, "/")], [], None)


async def test_unavailable_status_is_not_cached_as_a_denial():
    """One transient 403 must not outlive the failure TTL: the entry expires on
    the short clock, so a later 200 is honored on the next load."""
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        decode, calls = _decode_queue((None, 403))
        await load_robots_rules(
            canonical("https://waf.test"), object(), 5, fetch_and_decode=decode, policy=policy
        )
        delta = robots_cache.time_until_expiry(("https://waf.test", policy))
        assert delta is not None and delta <= ROBOTS_FAILURE_TTL_SECONDS
        # The short-lived entry still serves the window without re-fetching.
        again = await load_robots_rules(
            canonical("https://waf.test"), object(), 5, fetch_and_decode=decode, policy=policy
        )
        assert again == ([], [], None)
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
        robots_cache.put(("https://waf.test", policy), ([], [], None), ttl=-1.0)
        decode, calls = _decode_queue(("User-agent: *\nDisallow: /private\n", 200))
        rules = await load_robots_rules(
            canonical("https://waf.test"), object(), 5, fetch_and_decode=decode, policy=policy
        )
        assert rules[0] == [(False, "/private")]
        assert len(calls) == 1
    finally:
        robots_cache.clear()


async def test_robots_5xx_still_fails_closed():
    """RFC 9309 2.3.1.4: 5xx means unreachable -- complete disallow."""
    rules, _ = await _load("https://down.test", (None, 500))
    assert rules == ([(False, "/")], [], None)


async def test_robots_5xx_refusal_ttl_is_the_short_failure_ttl():
    policy = HostPolicy.unrestricted()
    robots_cache.clear()
    try:
        decode, _ = _decode_queue((None, 500))
        await load_robots_rules(
            canonical("https://down.test"), object(), 5, fetch_and_decode=decode, policy=policy
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
