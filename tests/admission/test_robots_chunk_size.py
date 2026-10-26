"""The robots admission gather is chunk-sized to one whole batch (#354)."""

import asyncio

from app.admission.admission_links import gather_robots_decisions
from tests._policy_factories import canonical


async def test_a_full_batch_resolves_in_one_gather():
    """max_batch_size (50) distinct origins: every check is in flight together
    -- a 16-chunked gather would peak at 16 and serialize four chunks behind
    each other; the batch cap makes one robots pass one gather."""
    urls = [canonical(f"http://origin{i}.example/") for i in range(50)]

    in_flight = 0
    peak = 0

    async def allows(url):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0)
        in_flight -= 1
        return True

    decisions = await gather_robots_decisions(urls, allows)
    assert peak == 50
    assert decisions == [(True, None)] * 50
