"""Robots batch admission must be bounded by a wall clock, not chunks × the
per-check robots timeout (#343); #366 extends that bound to the hot
link-admission path, whose robots gather otherwise hangs chunks × timeout."""

import asyncio
import time

from crawlutron.admission.admission_links import gather_robots_decisions
from tests._policy_factories import admit, canonical


async def test_robots_admission_pass_is_wall_clock_bounded():
    """A chunk pinned on a slow origin must not hang the whole admission pass
    for chunks × timeout: the per-chunk deadline fires, the chunk fails closed,
    and the pass returns well before the slow checks would."""
    # Speed the deadline so the test runs in milliseconds, not timeout+5.
    urls = [canonical(f"http://slow{i}.example/") for i in range(3)]

    async def allows(url):
        # A genuinely-blocking robots check (not asyncio.sleep, which the suite
        # zeroes out by default): hangs until the chunk deadline cancels it.
        await asyncio.Event().wait()
        return True

    started = time.monotonic()
    decisions = await asyncio.wait_for(
        gather_robots_decisions(urls, allows, timeout_seconds=0.05, deadline_slack_seconds=0.05),
        timeout=5.0,
    )
    elapsed = time.monotonic() - started

    # The three URLs share one chunk; the deadline fires once, failing them all
    # closed so the pass is bounded rather than chunks × 30s of hanging.
    assert elapsed < 2.0
    assert decisions == [(False, None), (False, None), (False, None)]


async def test_link_admission_hung_robots_fails_closed_within_deadline():
    """Link admission's robots gather (#366) carries the same per-chunk
    wall-clock deadline: a hung robots_allows fails the chunk closed within
    the deadline instead of hanging admission."""
    links = [f"http://stuck{i}.example/page" for i in range(2)]

    async def allows(url):
        await asyncio.Event().wait()
        return True

    started = time.monotonic()
    outcome = await asyncio.wait_for(
        admit(
            links,
            frontier=[],
            max_pages=10,
            robots_allows=allows,
            robots_timeout_seconds=0.05,
            robots_deadline_slack_seconds=0.05,
        ),
        timeout=5.0,
    )
    elapsed = time.monotonic() - started

    assert elapsed < 2.0
    # Both candidates failed closed: nothing admitted, two robots blocks.
    assert (outcome.accepted, outcome.robots_blocked) == (0, 2)


async def test_link_admission_without_deadline_waits_for_checks():
    """No timeout stated, no deadline: the legacy gather still waits for every
    check, so the fast path is unchanged when a caller passes nothing."""
    links = ["http://quick.example/page"]

    async def allows(url):
        return True

    outcome = await admit(links, frontier=[], max_pages=10, robots_allows=allows)
    assert (outcome.accepted, outcome.robots_blocked) == (1, 0)
