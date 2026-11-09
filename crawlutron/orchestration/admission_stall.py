"""Bounded no-progress guard for the recursive admission loop.

A saturated admission yields (0,0) forever; this owns the deadline that
stops and reports that stall instead of spinning.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple, cast

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_task import CrawlTask
from crawlutron.models.taxonomy import CrawlError
from crawlutron.runtime.metrics import MetricsStore
from crawlutron.transport.results import TerminalResult, failed_engine_result
from crawlutron.vocabulary.taxonomy import CrawlClassification

if TYPE_CHECKING:
    from crawlutron.orchestration.recursive import CrawlRun, CrawlState

# How long a recursion tolerates admission making NO progress before it stops.
NO_PROGRESS_STALL_SECONDS: float = 60.0


def spill_result(run: CrawlRun, index: int, result: EngineCrawlResult) -> Awaitable[None]:
    """One result to the crawl's spill rows; a None spill drops it (a caller that
    disabled spilling owns that answer)."""
    if run.spill is None:
        return _noop()
    return run.spill.aput(run.request_id, index, result)


async def _noop() -> None:
    return None


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


async def fail_remaining_frontier(run: CrawlRun, frontier: deque[tuple[int, CrawlTask]]) -> None:
    """Report every frontier URL as a capacity-stalled failure.

    The URLs were never dispatched, so dropping them would mask the stall.
    """
    while frontier:
        _depth, job = frontier.popleft()
        await spill_result(
            run,
            job.index,
            failed_engine_result(
                TerminalResult(
                    original_url=job.original_url,
                    normalized_url=job.normalized_url,
                    strategy=None,
                    classification=CrawlClassification.browser_pool_exhausted,
                    error=cast("CrawlError", CrawlClassification.browser_pool_exhausted),
                )
            ),
        )


type _AdmitFn = Callable[[CrawlRun, CrawlState, MetricsStore], Awaitable[list[tuple[int, CrawlTask]]]]
type _CrawlFn = Callable[
    [CrawlRun, list[tuple[int, CrawlTask]]],
    Awaitable[dict[int, EngineCrawlResult] | None],
]
type _CommitFn = Callable[
    [CrawlRun, CrawlState, list[tuple[int, CrawlTask]], dict[int, EngineCrawlResult] | None],
    Awaitable[None],
]


class BatchStages(NamedTuple):
    """The three batch stages, injected so this module imports nothing back."""

    start_runner: _AdmitFn
    crawl_batch: _CrawlFn
    commit_batch: _CommitFn


async def crawl_batches(
    run: CrawlRun,
    state: CrawlState,
    stall_guard: AdmissionStallGuard,
    shutdown_event: asyncio.Event,
    metrics: MetricsStore,
    max_pages: int,
    stages: BatchStages,
) -> None:
    """Drive admission batches until drain / cap / shutdown / admission-stall."""
    while state.frontier and (max_pages <= 0 or state.page_count < max_pages):
        if shutdown_event.is_set():
            return
        page_count_before = state.page_count
        allowed_jobs = await stages.start_runner(run, state, metrics)
        if not allowed_jobs and stall_guard.stalled():
            if run.spill is not None:
                await fail_remaining_frontier(run, state.frontier)
            return
        if not allowed_jobs:
            # Zero-admission pass: yield the turn -- never hot-spin the gates.
            await asyncio.sleep(0)
            continue
        scheduled_results = await stages.crawl_batch(run, allowed_jobs)
        await stages.commit_batch(run, state, allowed_jobs, scheduled_results)
        if allowed_jobs or state.page_count > page_count_before:
            stall_guard.record_progress()


__all__ = [
    "AdmissionStallGuard",
    "BatchStages",
    "NO_PROGRESS_STALL_SECONDS",
    "crawl_batches",
    "fail_remaining_frontier",
    "spill_result",
]
