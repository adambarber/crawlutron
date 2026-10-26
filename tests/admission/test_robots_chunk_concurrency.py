"""Robots admission chunks run bounded-concurrent, not serial (#544/P10)."""

import asyncio

from app.admission.admission_links import gather_robots_decisions
from app.models.normalized_url import NormalizedUrl
from tests._policy_factories import canonical


def _urls(n: int) -> list[NormalizedUrl]:
    return [canonical(f"http://x.test/p{i}") for i in range(n)]


def test_chunks_overlap_in_flight():
    """Within a group, chunk 1 starts before chunk 0's checks all end (#544/P10)."""
    events: list[str] = []

    async def slow_check(url: NormalizedUrl) -> bool:
        events.append(f"start:{url.raw}")
        await asyncio.sleep(0.01)
        events.append(f"end:{url.raw}")
        return True

    urls = _urls(300)
    asyncio.run(gather_robots_decisions(urls, slow_check, timeout_seconds=5.0))
    starts = [e for e in events if e.startswith("start:")]
    # Chunk 0 = urls 0-49; chunk 1 starts at url 50. Serially, all 50 of
    # chunk 0's ends precede it; concurrently, some chunk-0 ends come after.
    chunk1_first_start = events.index("start:" + starts[50].split(":", 1)[1])
    chunk0_ends_before = sum(1 for e in events[:chunk1_first_start] if e.startswith("end:"))
    assert chunk0_ends_before < 50, (
        f"all {chunk0_ends_before} chunk-0 checks ended before chunk 1 started: chunks are serial"
    )
