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


class _Granted(NamedTuple):
    allocation: Allocation
    scale: Scale = Scale(1.0)


class _LeaseTrackingAdmission:
    """Counts live leases the way the shared governor's in-flight count does."""

    def __init__(self) -> None:
        self.in_flight = 0

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
            yield cast("Granted", _Granted(allocation))
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
