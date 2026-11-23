"""One process-wide RAM pool bounds every concurrent crawl (#1023)."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from crawlutron_admit.sensing.pressure_memory import MemoryStats

_POLICY = {
    "limits": {"max_depth": 0, "respect_robots": False},
    "strategies": ["HTTP_DECLARED_CRAWLER"],
}
_TINY_RAM = 256 * 1024 * 1024


async def test_concurrent_crawls_divide_one_ram_derived_pool(monkeypatch):
    """Two crawls under a one-crawl budget must not both fetch at once."""
    from crawlutron.orchestration import resource_governor as rg
    from crawlutron.orchestration.engine import crawl_request
    from tests._admission_doubles import simple_page_route
    from tests._fetch_doubles import FetcherDouble

    def tiny() -> MemoryStats:
        return MemoryStats(total_bytes=_TINY_RAM, used_bytes=0)

    monkeypatch.setattr(rg, "read_memory_stats", tiny)
    monkeypatch.setattr(rg, "derive_browser_pool_size", lambda: 1)

    active = 0
    peak = 0

    async def observing_answer(url, headers, timeout, cap):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        try:
            await asyncio.sleep(0.02)
            return simple_page_route(url, headers, timeout, cap)
        finally:
            active -= 1

    fetcher = FetcherDouble(observing_answer)
    await asyncio.gather(
        *(
            crawl_request(
                {"urls": [f"http://{host}.example/page"], "policy": _POLICY},
                http_fetcher=fetcher,
                cffi_fetcher=fetcher,
                browser_worker=None,
            )
            for host in ("a", "b")
        )
    )
    assert peak == 1, f"two crawls used one RAM budget each (peak {peak})"


class _RecordingGovernor:
    """Wraps the real gate, counting consultations and concurrent leases."""

    def __init__(self, gate):
        self._gate = gate
        self.calls = 0
        self.active = 0
        self.peak = 0

    @asynccontextmanager
    async def admit(self, want):
        async with self._gate.admit(want) as granted:
            self.calls += 1
            self.active += 1
            self.peak = max(self.peak, self.active)
            try:
                yield granted
            finally:
                self.active -= 1


async def test_every_crawl_uses_the_injected_governor(monkeypatch):
    """Concurrent crawls consult one injected governor, never a private copy."""
    from crawlutron.orchestration import resource_governor as rg
    from crawlutron.orchestration.engine import crawl_request, resource_gate
    from crawlutron.orchestration.resource_governor import install_resource_governor
    from tests._admission_doubles import simple_page_route
    from tests._fetch_doubles import FetcherDouble

    def tiny() -> MemoryStats:
        return MemoryStats(total_bytes=_TINY_RAM, used_bytes=0)

    monkeypatch.setattr(rg, "read_memory_stats", tiny)
    monkeypatch.setattr(rg, "derive_browser_pool_size", lambda: 1)

    governor = _RecordingGovernor(rg.build_resource_governor(resource_gate()))
    install_resource_governor(governor)

    async def answer(url, headers, timeout, cap):
        await asyncio.sleep(0.01)
        return simple_page_route(url, headers, timeout, cap)

    fetcher = FetcherDouble(answer)
    await asyncio.gather(
        *(
            crawl_request(
                {"urls": [f"http://{host}.example/page"], "policy": _POLICY},
                http_fetcher=fetcher,
                cffi_fetcher=fetcher,
                browser_worker=None,
            )
            for host in ("a", "b")
        )
    )
    assert governor.calls == 2, f"only {governor.calls} crawls consulted the governor"
    assert governor.peak == 1, f"{governor.peak} leases were live at once"
