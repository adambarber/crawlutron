"""One fan-out law: the admission pass robots-gather honors the chunk bound (#544 N3)."""

from tests._policy_factories import admit


async def test_admission_pass_robots_gather_respects_chunk_bound():
    """120 distinct-origin candidates must fan out at most one chunk (50) at
    a time -- the pass may not gather the whole candidate set at once."""
    links = [f"http://origin{i}.example/" for i in range(120)]
    probe = _PeakProbe()

    await admit(links, frontier=[], max_pages=200, robots_allows=probe.allows)
    assert probe.peak == 50


class _PeakProbe:
    """Counts concurrent robots checks; shared shape with the #354 batch test."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0

    async def allows(self, url):
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        await _yield_once()
        self.in_flight -= 1
        return True


async def _yield_once():
    import asyncio

    await asyncio.sleep(0)
