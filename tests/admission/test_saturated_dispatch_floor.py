"""The sync batch's saturated-dispatch floor (#618): under a saturated gate the floor is the only progress
mechanism -- a floor of one forces a full re-bucket + admit cycle per dispatched job, one sequential pass
per task. The floor dispatches a fraction of the HTTP ramp per pass.
"""

from __future__ import annotations

from crawlutron.orchestration import request_pipeline
from crawlutron.runtime.pressure_sizing import AdmissionPolicy, WeightedAdmission
from tests._admission_doubles import run_sync_batch


async def test_saturated_floor_dispatches_more_than_one_per_pass(monkeypatch):
    """#618: under a never-admitting gate the floor pace is k = ramp/4 per pass,
    so an N-job batch costs ceil(N/k) admit cycles, not N."""
    zero_admits = {"calls": 0}
    chunks: list[int] = []

    async def zero_admit(self, want_http: int, want_browser: int) -> tuple[int, int]:
        zero_admits["calls"] += 1
        return 0, 0

    monkeypatch.setattr(WeightedAdmission, "admit", zero_admit)

    original_run = request_pipeline.DomainScheduler.run

    async def run_spy(self, jobs, worker, *, on_result=None, failed_result=None):
        chunks.append(len(jobs))
        await original_run(self, jobs, worker, on_result=on_result, failed_result=failed_result)

    monkeypatch.setattr(request_pipeline.DomainScheduler, "run", run_spy)

    out = await run_sync_batch(
        monkeypatch,
        n_urls=12,
        policy={"limits": {"max_depth": 0}, "strategies": ["HTTP_DECLARED_CRAWLER"]},
    )

    assert len(out["results"]) == 12, "every submitted page must return a result"
    assert chunks and min(chunks) > 1, (
        f"the saturated floor dispatched one job per pass ({chunks}): a 2000-task "
        f"batch under pressure pays one re-bucket + admit cycle per job"
    )
    assert sum(chunks) == 12, "the floor never drops or duplicates a job"


def test_floor_is_a_fraction_of_the_http_ramp() -> None:
    """The floor scales with the ramp instead of a magic constant."""
    gate = request_pipeline.ResourceGate(target_mem=0.9, memory_ratio=lambda: 0.0, cpu_ratio=lambda: 0.0)

    def admission(ramp: int) -> WeightedAdmission:
        policy = AdmissionPolicy(
            http_task_bytes=0,
            browser_task_bytes=0,
            browser_pool_reserve_bytes=0,
            max_http_concurrency=ramp,
            max_browser_concurrency=1,
        )
        return WeightedAdmission(gate, policy)

    assert admission(200).saturated_dispatch_floor() == 50
    assert admission(3).saturated_dispatch_floor() == 1
