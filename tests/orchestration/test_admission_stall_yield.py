"""The zero-admission dispatch pass must yield, not hot-spin the gates (#526).

A saturated kernel admit never returns (0,0) -- it parks inside or grants a
floor, so the empty-pass is a robots-filtered batch, and it must still yield."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from crawlutron.orchestration.admission_stall import AdmissionStallGuard, BatchStages, crawl_batches
from crawlutron.orchestration.recursive import (
    admit_recursive_batch,
    commit_recursive_results,
    crawl_recursive_batch,
)
from crawlutron.runtime.metrics import get_metrics
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import build_admission_gate
from tests._admission_runs import minimum_admission_run
from tests._recursive_harnesses import make_crawl_task


class _ZeroGate(ResourceGate):
    """A real ResourceGate reporting zero use, wired to the kernel gate.

    Stays a genuine ``ResourceGate`` subclass: the sanctioned double the
    doubles-inventory docstring flags explicitly, not a duck."""

    def memory_ratio(self) -> float:
        return 0.0

    def cpu_ratio(self) -> float:
        return 0.0

    def batch_scale_factor(self) -> float:
        return 1.0


async def _refuse_all(url):
    return False


async def _refused_result(job):
    from tests._result_builders import engine_result

    return engine_result(job.normalized_url, "robots_disallowed", status="skipped")


async def test_zero_admission_pass_yields_instead_of_hot_spinning(monkeypatch):
    """An empty pass (robots-filtered, not a (0,0) admission) yields to the loop.

    Until the stall deadline fires the loop owes the event loop a turn per empty
    pass instead of hot gate-sampling (#526)."""
    delays: list[float] = []
    real_sleep = asyncio.sleep

    async def sleep_spy(delay=None):
        delays.append(delay)
        return await real_sleep(delay)

    monkeypatch.setattr("crawlutron.orchestration.admission_stall.asyncio.sleep", sleep_spy)
    jobs = [make_crawl_task(0, "a"), make_crawl_task(1, "a")]
    base_run, state, _ = minimum_admission_run(jobs, http_quota=8, browser_quota=0)
    run = replace(
        base_run,
        respect_robots=True,
        robots_allows=_refuse_all,
        robots_disallowed=_refused_result,
        admission=build_admission_gate(_ZeroGate()),
    )
    guard = AdmissionStallGuard(clock=lambda: 0.0, no_progress_seconds=60.0)
    stages = BatchStages(admit_recursive_batch, crawl_recursive_batch, commit_recursive_results)
    await crawl_batches(run, state, guard, asyncio.Event(), get_metrics(), 50, stages)
    assert 0 in delays, f"the empty pass did not yield a sleep(0) to the loop: {delays}"
