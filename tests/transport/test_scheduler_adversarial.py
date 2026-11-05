"""#577 adversarial findings, pinned as behavior (F1, F2).

F1: the purge must never drop a record whose gate is in use -- a re-minted
gate is FULL, so the per-domain concurrency cap silently widens mid-crawl.
F2: jobs waking from a 429 cooldown nap re-reserve, or the wake-up instant
is itself a herd.
"""

from __future__ import annotations

import asyncio
from typing import TypedDict

import pytest

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.transport.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler
from tests._result_builders import engine_result, make_job
from tests._scheduler_doubles import engine_envelope


class _FloorCheck(TypedDict):
    """What the mid-storm probe records about the committed floor."""

    floor_after_storm: bool


def _scheduler(**overrides: object) -> DomainScheduler:
    base: dict[str, object] = {
        "global_limit": 10,
        "per_domain_limit": 1,
        "delay_seconds": 0.0,
        "adaptive": False,
    }
    base.update(overrides)
    # The four knobs the test scheduler actually varies; isinstance narrows each.
    assert isinstance(base["global_limit"], int)
    assert isinstance(base["per_domain_limit"], int)
    assert isinstance(base["delay_seconds"], float)
    assert isinstance(base["adaptive"], bool)
    return DomainScheduler(
        global_limit=base["global_limit"],
        per_domain_limit=base["per_domain_limit"],
        delay_seconds=base["delay_seconds"],
        adaptive=base["adaptive"],
    )


def _jobs(n: int) -> list[CrawlJob]:
    return [make_job(i, f"http://example.com/{i}") for i in range(n)]


def _instant_sleep_shim(monkeypatch) -> None:
    """Every sleep becomes a single zero-second yield (suite zero-wall doctrine)."""
    real_sleep = asyncio.sleep

    async def instant_sleep(delay, *args, **kwargs):
        del delay, args, kwargs
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


class TestPurgeNeverDropsAnInUseGate:
    """F1: record liveness includes 'the gate is held or waited on right now';
    dropping it re-mints a FULL gate under the live holders and the cap widens."""

    @pytest.mark.parametrize("limit", [1, 2])
    async def test_a_mid_worker_cadence_purge_keeps_the_cap(self, monkeypatch, limit):
        _instant_sleep_shim(monkeypatch)
        scheduler = _scheduler(per_domain_limit=limit)
        in_flight = 0
        peak = 0

        async def worker(job):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            if job.index == 0:
                # While this worker holds a gate slot, a 429 storm drives the
                # amortized purge over the held record.
                for _ in range(COOLDOWN_PURGE_INTERVAL):
                    scheduler.set_domain_cooldown("other.test", 1.0)
            await asyncio.sleep(0.01)
            in_flight -= 1
            return engine_result(job.url.raw, "rendered")

        results = await scheduler.run(_jobs(4), worker, failed_result=engine_envelope)
        assert results is not None and len(results) == 4
        assert peak <= limit, f"a mid-worker purge widened the gate: peak {peak} at per_domain_limit={limit}"

    async def test_the_purged_mid_run_record_keeps_its_spacing_state(self, monkeypatch):
        """The politeness corollary: the same storm must not erase next_fire,
        min_delay, or the bucket of a record a worker is using."""
        _instant_sleep_shim(monkeypatch)
        scheduler = _scheduler()
        seen: list[_FloorCheck] = []

        async def worker(job):
            if job.index == 0:
                scheduler.set_domain_min_delay("example.com", 0.05)
                for _ in range(COOLDOWN_PURGE_INTERVAL):
                    scheduler.set_domain_cooldown("other.test", 1.0)
                seen.append({"floor_after_storm": scheduler.current_delay("example.com") >= 0.05})
            await asyncio.sleep(0)
            return engine_result(job.url.raw, "rendered")

        await scheduler.run(_jobs(2), worker, failed_result=engine_envelope)
        assert seen and seen[0]["floor_after_storm"] is True, "the committed floor was purged mid-worker"


class TestCooldownWakeReReserves:
    """F2: peers waking from the same 429 cooldown must still space their
    fires -- the wake instant is not a synchronized free-for-all. Suite rules:
    conftest patches asyncio.sleep to zero-second yields, so spacing is
    observed through the REQUESTED waits (SleepRecorder), never wall time.
    """

    async def test_peers_waking_from_one_cooldown_re_reserve(self, monkeypatch):
        """Zero real sleep: the ONE injected clock advances whenever a wait is
        requested -- cooldown naps and spacing waits share it since #580, so
        every requested sleep just moves the clock. The 429 expiry passes
        deterministically and instantly."""
        real_sleep = asyncio.sleep
        requested: list[float] = []
        now = {"t": 1000.0}
        scheduler = _scheduler(delay_seconds=0.15, clock=lambda: now["t"])
        started = asyncio.Event()

        async def fake_clocked_sleep(delay, *args, **kwargs):
            del args, kwargs
            requested.append(delay)
            # Every wait is on the one clock now: any requested delay just
            # moves it (zero real time).
            if delay > 0:
                now["t"] += delay
            await real_sleep(0)

        monkeypatch.setattr(asyncio, "sleep", fake_clocked_sleep)

        async def worker(job):
            if job.index == 0:
                # A 429 lands while the batch is spacing: everyone converges
                # on this expiry.
                scheduler.set_domain_cooldown("example.com", 0.1)
                started.set()
            if job.index > 0:
                await started.wait()
            await real_sleep(0)
            return engine_result(job.url.raw, "rendered")

        results = await scheduler.run(_jobs(3), worker, failed_result=engine_envelope)
        assert results is not None and len(results) == 3
        # Reserve slots are delay-scaled and stagger; cooldown naps are ~0.1.
        # The contract is stated on the delay-scaled waits alone.
        spacing = sorted(d for d in requested if d > 0.11)
        assert len(spacing) >= 2, f"cooldown-wake peers must each re-reserve a spacing wait: {requested}"
        assert all(spacing[i + 1] > spacing[i] for i in range(len(spacing) - 1)), (
            f"spacing waits must stagger cumulatively, not copy: {spacing}"
        )
