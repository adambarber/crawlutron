"""A robots-stage failure must release the recursive batch's admission lease (#1023)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import replace
from typing import NamedTuple, cast

import pytest

from crawlutron.orchestration.recursive import admit_recursive_batch
from crawlutron.runtime.metrics import get_metrics
from crawlutron_admit import Allocation, AllocationEntry, Count, Granted, LaneId, Scale, Want
from safe_url.normalized_url import NormalizedUrl
from tests._admission_runs import minimum_admission_run
from tests._recursive_harnesses import make_crawl_task

_UNIT_SCALE = Scale(1.0)


class _Granted(NamedTuple):
    allocation: Allocation
    scale: Scale = Scale(1.0)


class _LeaseTrackingAdmission:
    """Counts live leases the way the shared governor's in-flight count does."""

    def __init__(self, scale: Scale = _UNIT_SCALE) -> None:
        self.in_flight = 0
        self.scale = scale

    @asynccontextmanager
    async def admit(self, want: Want) -> AsyncGenerator[Granted]:
        self.in_flight += 1
        try:
            allocation = Allocation(
                {
                    LaneId.GENERAL: AllocationEntry(Count(want.lanes[LaneId.GENERAL].value)),
                    LaneId.HEAVY: AllocationEntry(Count(want.lanes[LaneId.HEAVY].value)),
                }
            )
            yield cast("Granted", _Granted(allocation, self.scale))
        finally:
            self.in_flight -= 1


async def _deny(url: NormalizedUrl) -> bool:
    return False


async def _explode(task: object) -> object:
    raise RuntimeError("robots stage exploded")


async def test_robots_stage_failure_releases_the_admission_lease() -> None:
    """A throw at the robots stage closes the lease before propagating."""
    tasks = [make_crawl_task(0, "a")]
    run, state, _ = minimum_admission_run(tasks, http_quota=10, browser_quota=10)
    tracking = _LeaseTrackingAdmission()
    run = replace(
        run,
        admission=tracking,
        respect_robots=True,
        robots_allows=_deny,
        robots_disallowed=_explode,
    )

    with pytest.raises(RuntimeError, match="robots stage exploded"):
        await admit_recursive_batch(run, state, get_metrics())

    assert tracking.in_flight == 0, "the failed robots stage stranded a live admission lease"


async def test_throttle_metric_failure_releases_the_admission_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """A raise after admission succeeds must still close the lease exactly once."""
    tasks = [make_crawl_task(0, "a")]
    run, state, _ = minimum_admission_run(tasks, http_quota=10, browser_quota=10)
    tracking = _LeaseTrackingAdmission(scale=Scale(0.5))
    metrics = get_metrics()

    async def explode() -> None:
        raise RuntimeError("throttle metric exploded")

    monkeypatch.setattr(metrics, "record_throttle", explode)
    run = replace(run, admission=tracking, respect_robots=False)

    with pytest.raises(RuntimeError, match="throttle metric exploded"):
        await admit_recursive_batch(run, state, metrics)

    assert tracking.in_flight == 0, "the failed throttle metric stranded a live admission lease"
