"""An open breaker must PARK the batch (domain-cooldown path), not requeue it (#526).

Failing each job requeued it up to 3x into the still-open breaker: synthetic
retry storms, zero fetches, transient_requeues polluted."""

import asyncio

from app.models.crawl_task import CrawlTask
from app.orchestration.scheduler import DomainScheduler
from tests._policy_factories import canonical
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task
from tests._result_builders import make_engine_result
from tests._scheduler_doubles import engine_envelope, make_scheduler, ok_worker, trip_circuit


def _task(index: int, url: str) -> CrawlTask:
    return CrawlTask(index=index, original_url=url, normalized_url=canonical(url))


class _Clock:
    """A mutable monotonic clock the test advances when the park sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _allows(url):
    return True


async def test_open_breaker_parks_the_batch_and_records_a_cooldown(monkeypatch):
    """A batch facing an open breaker parks (cooldown recorded), then RUNS when
    the window elapses -- no failed envelope, no requeue, no lost fetch."""
    clock = _Clock()
    monkeypatch.setattr("app.orchestration.scheduler.time.monotonic", clock)
    requested: list[float] = []
    real_sleep = asyncio.sleep

    async def advance_on_sleep(delay=None):
        # Record the park request and move the injected clock past the window,
        # so the half-open recovery is deterministic and zero wall clock.
        if delay:
            requested.append(delay)
            clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr("app.orchestration.scheduler.asyncio.sleep", advance_on_sleep)
    scheduler = make_scheduler(circuit_cooldown_base=30.0)
    trip_circuit(scheduler, "example.com")

    ran = []

    async def worker(job):
        ran.append(job.index)
        return await ok_worker(job)

    job = _task(0, "http://example.com/a")
    results = await scheduler.run([job], worker, failed_result=engine_envelope)

    # The window was parked out (the sleep was requested), a cooldown was
    # recorded, and after it elapsed the worker actually ran -- success.
    assert requested and max(requested) >= 29.0, (
        "the batch facing an open breaker did not park: no sleep of the breaker "
        f"window was requested (sleeps: {requested})"
    )
    assert "example.com" in scheduler.domain_cooldowns, (
        "the open breaker did not record a domain cooldown: later batches will "
        "re-dispatch straight into the breaker instead of parking"
    )
    assert ran == [0], f"the job did not run after the park (worker ran: {ran})"
    assert results is not None and results[0].status == "success", results


async def test_breaker_still_open_after_the_park_cap_fails_the_job(monkeypatch):
    """Past the park cap (or a re-asserted window) the envelope is terminal:
    the job fails once through the classified channel, exactly as before."""
    scheduler = make_scheduler(circuit_cooldown_base=30.0)
    trip_circuit(scheduler, "example.com")

    async def worker(job):
        raise AssertionError(f"the worker must not run while the breaker is open: {job}")

    job = _task(0, "http://example.com/a")
    results = await scheduler.run([job], worker, failed_result=engine_envelope)

    assert results is not None
    assert results[0].classification == "circuit_breaker_open", results
    assert results[0].diagnostics.get("circuit_breaker") is True, results.diagnostics


async def test_recursive_batch_over_an_open_breaker_runs_not_requeues(monkeypatch):
    """The storm end-to-end: a recursive batch over an open breaker must run each
    job once after parking -- no circuit_breaker_open envelopes, no requeue churn."""
    clock = _Clock()
    monkeypatch.setattr("app.orchestration.scheduler.time.monotonic", clock)
    real_sleep = asyncio.sleep

    async def advance_on_sleep(delay=None):
        # The park sleeps out the breaker window; advancing the injected clock
        # here makes the half-open recovery deterministic, zero wall clock.
        if delay:
            clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr("app.orchestration.admission_stall.asyncio.sleep", advance_on_sleep)

    scheduler = DomainScheduler(
        global_limit=4, per_domain_limit=4, delay_seconds=0.0, circuit_cooldown_base=30.0
    )
    trip_circuit(scheduler, "a")
    ran = []

    async def worker(job):
        ran.append(job.index)
        return make_engine_result(job.normalized_url)

    result = await drive_recursive_crawl(
        [make_crawl_task(0, "a"), make_crawl_task(1, "a")],
        worker,
        allows=_allows,
        scheduler=scheduler,
    )

    assert sorted(ran) == [0, 1], (
        "the recursive batch did not run each job exactly once across the open "
        f"breaker: {ran} (requeue storm re-dispatches would repeat or skip)"
    )
    assert all(r.status == "success" for r in result.spill.writes.values()), (
        f"expected successes after parking out the window, got {result.spill.writes}"
    )
