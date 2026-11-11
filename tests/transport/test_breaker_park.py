"""An open breaker must PARK the batch (domain-cooldown path), not requeue it (#526).

Failing each job requeued it up to 3x into the still-open breaker: synthetic
retry storms, zero fetches, transient_requeues polluted."""

import asyncio
import re

import pytest

from crawlutron.models.crawl_task import CrawlTask
from crawlutron.transport.scheduler import DomainScheduler
from tests._clocks import Timeline, parking_clock
from tests._policy_factories import canonical
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task
from tests._result_builders import engine_result
from tests._scheduler_doubles import engine_envelope, make_scheduler, ok_worker, trip_circuit


def _task(index: int, url: str) -> CrawlTask:
    return CrawlTask(index=index, original_url=url, normalized_url=canonical(url))


async def _allows(url):
    return True


async def test_open_breaker_parks_the_batch_and_records_a_cooldown(monkeypatch):
    """A batch facing an open breaker parks (cooldown recorded), then RUNS when
    the window elapses -- no failed envelope, no requeue, no lost fetch."""
    clock, requested = parking_clock(monkeypatch)
    scheduler = make_scheduler(circuit_cooldown_base=30.0, clock=clock)
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


async def _terminal_envelope_results(*, base: float):
    """Trip the breaker, run one batch, hand back the terminal results: the
    shared shape of "the breaker stands and the job fails classified once"."""
    scheduler = make_scheduler(circuit_cooldown_base=base)
    trip_circuit(scheduler, "example.com")

    async def worker(job):
        raise AssertionError(f"the worker must not run while the breaker is open: {job}")

    job = _task(0, "http://example.com/a")
    return await scheduler.run([job], worker, failed_result=engine_envelope)


async def test_breaker_still_open_after_the_park_cap_fails_the_job(monkeypatch):
    """Past the park cap (or a re-asserted window) the envelope is terminal:
    the job fails once through the classified channel, exactly as before."""
    results = await _terminal_envelope_results(base=30.0)
    _assert_terminal(results)


async def test_recursive_batch_over_an_open_breaker_runs_not_requeues(monkeypatch):
    """The storm end-to-end: a recursive batch over an open breaker must run each
    job once after parking -- no circuit_breaker_open envelopes, no requeue churn."""
    clock = Timeline()
    real_sleep = asyncio.sleep

    async def advance_on_sleep_local(delay=None):
        # The park sleeps out the breaker window; advancing the injected clock
        # here makes the half-open recovery deterministic, zero wall clock.
        if delay:
            clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr("crawlutron.orchestration.admission_stall.asyncio.sleep", advance_on_sleep_local)

    scheduler = DomainScheduler(
        global_limit=4, per_domain_limit=4, delay_seconds=0.0, circuit_cooldown_base=30.0, clock=clock
    )
    trip_circuit(scheduler, "a")
    ran = []

    async def worker(job):
        ran.append(job.index)
        return engine_result(job.normalized_url, "rendered")

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


def _assert_terminal(results) -> None:
    """The terminal breaker envelope: classified once, breaker-named diagnostics."""
    assert results is not None
    assert results[0].classification == "circuit_breaker_open", results
    assert results[0].diagnostics.get("circuit_breaker") is True, results[0].diagnostics


async def test_breaker_window_over_the_park_cap_fails_terminal(monkeypatch):
    """A breaker window larger than the park cap parks only the cap, then the
    still-open re-check fails the job terminally -- no oversized wait waves the
    batch at the origin (#580 follow-up: the cap the park sleeps is pinned)."""
    clock, requested = parking_clock(monkeypatch)
    # Two trips: 200 then 400 (min(400, cooldown ceiling 600)) > the 300 cap.
    scheduler = make_scheduler(circuit_cooldown_base=200.0, circuit_cooldown=600.0, clock=clock)
    trip_circuit(scheduler, "example.com", failures=scheduler.circuit_threshold * 2)

    async def worker(job):
        raise AssertionError(f"the worker must not run while the breaker re-check stands: {job}")

    job = _task(0, "http://example.com/a")
    results = await scheduler.run([job], worker, failed_result=engine_envelope)

    assert requested == [300.0], f"the park must be capped at 300s: {requested}"
    # The STAMP matches the sleep: a cooldown outliving the breaker window by
    # the uncapped remainder would keep napping batches past half-open.
    assert scheduler.domain_cooldowns["example.com"] == pytest.approx(300.0), (
        f"the park must stamp the capped window, not the raw breaker remaining: {scheduler.domain_cooldowns}"
    )
    _assert_terminal(results)


async def test_the_breaker_skip_names_the_open_window_not_none():
    """The skip's exception names the domain and remaining window (#347): its text
    is the log's ``error_detail``, and "None" tells an operator nothing."""
    scheduler = make_scheduler(circuit_cooldown_base=30.0)
    trip_circuit(scheduler, "example.com")
    seen: list[BaseException] = []

    def envelope(job, error):
        seen.append(error)
        return engine_envelope(job, error)

    async def worker(job):
        raise AssertionError(f"the worker must not run while the breaker is open: {job}")

    results = await scheduler.run([_task(0, "http://example.com/a")], worker, failed_result=envelope)

    assert results is not None and results[0].classification == "circuit_breaker_open", results
    assert seen, "the skip never built an envelope"
    assert re.fullmatch(r"example\.com circuit open: \d+s of cooldown remain", str(seen[0])), str(seen[0])
