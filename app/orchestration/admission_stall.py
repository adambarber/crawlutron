"""Bounded no-progress guard for the recursive admission loop (#306).

A saturated admission yields (0,0) forever; this owns the deadline that
stops and reports that stall instead of spinning.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, cast

from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.models.taxonomy import Classification, CrawlError
from app.orchestration.results import TerminalResult, failed_engine_result
from app.vocabulary.taxonomy import CrawlClassification

if TYPE_CHECKING:
    from app.orchestration.recursive import RecursiveRunState, RecursiveState

# How long a recursion tolerates admission making NO progress before it stops.
NO_PROGRESS_STALL_SECONDS: float = 60.0


class AdmissionStallGuard:
    """No-progress deadline for one recursive run, on an injected clock.

    ``record_progress`` restarts it; ``stalled`` reports the bound passed.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        no_progress_seconds: float = NO_PROGRESS_STALL_SECONDS,
    ) -> None:
        self._clock = clock
        self._bound = no_progress_seconds
        self._last_progress = clock()

    def record_progress(self) -> None:
        self._last_progress = self._clock()

    def stalled(self) -> bool:
        return self._clock() - self._last_progress >= self._bound


async def fail_remaining_frontier(
    spill: Any,
    spill_write: Callable[[Any, int, EngineCrawlResult], Awaitable[None]],
    frontier: deque[tuple[int, CrawlTask]],
) -> None:
    """Report every frontier URL as a capacity-stalled failure.

    The URLs were never dispatched, so dropping them would mask the stall.
    """
    while frontier:
        _depth, job = frontier.popleft()
        await spill_write(
            spill,
            job.index,
            failed_engine_result(
                TerminalResult(
                    original_url=job.original_url,
                    normalized_url=job.normalized_url,
                    strategy=None,
                    classification=cast("Classification", CrawlClassification.browser_pool_exhausted),
                    error=cast("CrawlError", CrawlClassification.browser_pool_exhausted),
                )
            ),
        )


async def crawl_batches(
    run: RecursiveRunState,
    state: RecursiveState,
    stall_guard: AdmissionStallGuard,
    shutdown_event: asyncio.Event,
    metrics: Any,
    max_pages: int,
    *,
    start_runner: Callable[[RecursiveRunState, RecursiveState, Any], Awaitable[list[tuple[int, CrawlTask]]]],
    crawl_batch: Callable[
        [RecursiveRunState, list[tuple[int, CrawlTask]]],
        Awaitable[dict[int, EngineCrawlResult] | None],
    ],
    commit_batch: Callable[
        [RecursiveRunState, RecursiveState, list[tuple[int, CrawlTask]], dict[int, EngineCrawlResult] | None],
        Awaitable[None],
    ],
) -> None:
    """Drive admission batches until drain / cap / shutdown / admission-stall."""
    while state.frontier and (max_pages <= 0 or state.page_count < max_pages):
        if shutdown_event.is_set():
            return
        page_count_before = state.page_count
        allowed_jobs = await start_runner(run, state, metrics)
        if not allowed_jobs and stall_guard.stalled():
            await fail_remaining_frontier(run.spill, run.spill_write, state.frontier)
            return
        if not allowed_jobs:
            # Zero-admission pass: yield the turn -- never hot-spin the gates.
            await asyncio.sleep(0)
            continue
        scheduled_results = await crawl_batch(run, allowed_jobs)
        await commit_batch(run, state, allowed_jobs, scheduled_results)
        if allowed_jobs or state.page_count > page_count_before:
            stall_guard.record_progress()


__all__ = [
    "AdmissionStallGuard",
    "NO_PROGRESS_STALL_SECONDS",
    "crawl_batches",
    "fail_remaining_frontier",
]
