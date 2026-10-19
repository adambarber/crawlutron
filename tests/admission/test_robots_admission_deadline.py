"""Robots batch admission must be bounded by a wall clock, not chunks × the
per-check robots timeout (#343): a slow origin must not cost the whole pass."""

import asyncio
import time

from app.admission import admission_links
from app.admission.admission_links import gather_robots_decisions
from tests._policy_factories import canonical


async def test_robots_admission_pass_is_wall_clock_bounded(monkeypatch):
    """A chunk pinned on a slow origin must not hang the whole admission pass
    for chunks × timeout: the per-chunk deadline fires, the chunk fails closed,
    and the pass returns well before the slow checks would."""
    # Speed the deadline so the test runs in milliseconds, not timeout+5.
    monkeypatch.setattr(admission_links, "_ROBOTS_CHUNK_DEADLINE_SLACK_SECONDS", 0.05)
    urls = [canonical(f"http://slow{i}.example/") for i in range(3)]

    async def allows(url):
        # A genuinely-blocking robots check (not asyncio.sleep, which the suite
        # zeroes out by default): hangs until the chunk deadline cancels it.
        await asyncio.Event().wait()
        return True

    started = time.monotonic()
    decisions = await asyncio.wait_for(
        gather_robots_decisions(urls, allows, timeout_seconds=0.05),
        timeout=5.0,
    )
    elapsed = time.monotonic() - started

    # The three URLs share one chunk; the deadline fires once, failing them all
    # closed so the pass is bounded rather than chunks × 30s of hanging.
    assert elapsed < 2.0
    assert decisions == [(False, None), (False, None), (False, None)]
