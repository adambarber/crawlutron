"""The delegated crawl-side output record is page_count-only (R7-B1).
The legacy ``or allowed_jobs`` arm re-admitted granted-but-void passes as
progress; the seam stop requires actual produced pages, never an admit.
"""

import asyncio

from crawlutron.orchestration.admission_stall import (
    AdmissionStallGuard,
    BatchStages,
    crawl_batches,
)
from crawlutron.runtime.metrics import get_metrics
from tests._admission_runs import minimum_admission_run, task


class _CountingStallGuard(AdmissionStallGuard):
    """The same guard, counting ``record_progress`` so a probe can tell it fired."""

    def __init__(self) -> None:
        super().__init__(clock=lambda: 0.0, no_progress_seconds=60.0)
        self.recorded = 0

    def record_progress(self) -> None:
        self.recorded += 1
        super().record_progress()


async def test_granted_but_page_count_unchanged_records_no_progress() -> None:
    """A batch admitted and crawled but producing no committed page must NOT
    advance the crawl-side output-progress record: an admit is not output (R7-B1).
    Mutant (record_progress on ``or allowed_jobs``) raises this record to 1."""
    job = task("http://a.example/one")
    run, state, _admission = minimum_admission_run([job], http_quota=1, browser_quota=0)
    guard = _CountingStallGuard()
    frontier_consumed = False

    async def start_runner(_run, st, _metrics):
        nonlocal frontier_consumed
        if frontier_consumed:
            return []
        frontier_consumed = True
        st.frontier.clear()
        return [(0, job)]

    async def crawl_batch(_run, _allowed):
        return {job.index: None}

    async def commit_batch(_run, _st, _allowed, _results):
        return None

    await crawl_batches(
        run,
        state,
        guard,
        asyncio.Event(),
        get_metrics(),
        max_pages=10,
        stages=BatchStages(start_runner, crawl_batch, commit_batch),
    )

    assert guard.recorded == 0, "a granted-but-void pass must not count as output progress"
