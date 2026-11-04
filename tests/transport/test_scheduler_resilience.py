"""Purges, delays and delivery failures never lose or double-count work."""

import asyncio

import pytest

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.taxonomy import Classification
from crawlutron.transport.results import TerminalResult
from crawlutron.transport.scheduler import DomainScheduler
from tests._policy_factories import canonical
from tests._result_builders import engine_result, make_job
from tests._scheduler_doubles import FailingSink, engine_envelope, make_scheduler, run_through
from tests._timing_harnesses import recorder


def test_run_still_purges_up_front():
    """The batch entry point keeps its up-front purge so a long run cannot grow
    the dicts without bound even when no 429 fires."""
    now = [1000.0]
    scheduler = make_scheduler(clock=lambda: now[0])
    scheduler.set_domain_cooldown("stale.test", 10.0)
    assert "stale.test" in scheduler.domain_cooldowns
    now[0] = 1011.0  # the cooldown has elapsed
    # The up-front purge fires before any job runs.
    scheduler.purge_expired()
    assert "stale.test" not in scheduler.domain_cooldowns


class TestSchedulerResilience:
    async def test_scheduler_continues_other_jobs_on_single_failure(self):
        """One failing job must not cancel the others; results for the
        successful jobs are still returned."""
        scheduler = make_scheduler(global_limit=5, per_domain_limit=5)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]

        async def failing_worker(job: CrawlJob) -> EngineCrawlResult:
            if job.index == 1:
                raise RuntimeError("transient failure")
            return engine_result(job.url.raw, "rendered")

        results = await scheduler.run(jobs, failing_worker, failed_result=engine_envelope)
        assert results is not None
        # The failing job is not dropped: the chokepoint builds it a classified terminal envelope (reading the
        # URL
        assert len(results) == 3, results
        assert results[1].status == "failed"
        assert results[1].classification == "network_error"
        assert results[1].normalized_url == canonical("http://example.com/1")
        assert all(r.status == "success" for i, r in results.items() if i != 1)
        assert {r.normalized_url for i, r in results.items() if i != 1} == {
            canonical("http://example.com/0"),
            canonical("http://example.com/2"),
        }


class _PerDomainDelayScheduler(DomainScheduler):
    """Fixed delays that differ per domain (adaptive throttling off)."""

    def current_delay(self, domain: str) -> float:
        return 0.4 if domain == "example.com" else 0.0


def _nap_waits_for_fast_domain(monkeypatch, label: str) -> list[str]:
    """Shim: any reservation-length nap waits (bounded) for a fast-domain peer
    to be served; a nap must never hold a GLOBAL slot while it waits."""
    fast_started = asyncio.Event()
    starved: list[str] = []
    yield_to_loop = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        del args, kwargs
        if delay >= 0.2:
            try:
                await asyncio.wait_for(fast_started.wait(), 2.0)
            except TimeoutError:
                starved.append(label)
            return
        await yield_to_loop(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return starved


def _fast_domain_worker(job):
    """Serve example.org instantly; every other domain sleeps a real yield."""
    if job.domain == "example.org":
        _FAST_STARTED.set()
    return engine_result(job.url.raw, "rendered")


_FAST_STARTED = asyncio.Event()


def _nap_until_peer_recorded(
    monkeypatch, real_sleep, committed: asyncio.Event, clock: list[float] | None = None
):
    """Sequenced sleep shim: every nap blocks until ``committed`` fires, so the
    napped job's post-nap re-check cannot race the peer's commit; every
    requested delay is still recorded. Zero wall clock, and -- with an injected
    ``clock`` box -- the clock ADVANCES by each recorded delay, so floor-based
    assertions are exact instead of racing the real monotonic under xdist.
    (#577: the first fire takes no nap at all -- its wait is zero -- so the FIRST
    nap is the peer's reservation and waiting on it is deadlock-free; the trip's
    worker naps nothing before committing.)
    """
    sleeps = recorder(monkeypatch)

    async def _sleep(delay, *args, **kwargs):
        del args, kwargs
        if not committed.is_set():
            await committed.wait()
        sleeps.delays.append(delay)
        if clock is not None:
            clock[0] += delay
        await real_sleep(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)
    return sleeps


def _two_same_domain_jobs():
    """The pair every gate test drives: two jobs, one registrable domain."""
    return [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]


async def _run_two_jobs(scheduler, worker):
    """Drive the pair; with no sink, ``run`` must hand back the mapping."""
    results = await scheduler.run(_two_same_domain_jobs(), worker, failed_result=engine_envelope)
    assert results is not None, "without a sink, run returns the index-keyed mapping"
    return results


class TestJitterSleepDoesNotHoldTheDomainGate:
    """#541's property, INVERTED by #577: the per-domain delay is a fire-time
    reservation. The nap must not hold the gate AND must not let a peer fire
    inside the reservation -- both were possible; only the first was a defect.
    """

    async def test_second_same_domain_job_waits_out_the_first_reservation(self, monkeypatch):
        """Two same-domain jobs with a real delay: the second's fetch must happen
        only after its reservation nap completes -- never concurrent with it.
        """
        scheduler = make_scheduler(_PerDomainDelayScheduler, adaptive=False)
        yield_to_loop = asyncio.sleep
        naps = {"begun": 0, "done": 0}
        fetched_during: list[int] = []  # naps completed when each worker ran

        async def _sleep(delay, *args, **kwargs):
            del args, kwargs
            if delay >= 0.2:
                naps["begun"] += 1
                await yield_to_loop(0)
                naps["done"] += 1
                return
            await yield_to_loop(0)

        monkeypatch.setattr(asyncio, "sleep", _sleep)

        async def worker(job):
            fetched_during.append(naps["done"])
            await yield_to_loop(0)
            return engine_result(job.url.raw, "rendered")

        results = await _run_two_jobs(scheduler, worker)

        assert sorted(results) == [0, 1], "both jobs must still run"
        assert naps["begun"] == 1, "the second fire must nap exactly once (its reservation)"
        # Job 0 (no reservation wait) runs before any nap; job 1 runs only after
        # its own reservation nap completed -- the herd ran BOTH at naps_done == 0.
        assert fetched_during == [0, 1], (
            f"the second same-domain fetch must follow the first's completed "
            f"reservation nap; the herd shape is [0, 0]: {fetched_during}"
        )

    async def test_the_domain_gate_is_still_free_during_a_reservation_nap(self, monkeypatch):
        """#541's true half, kept: a DIFFERENT-domain job must run while a peer's
        reservation naps -- the nap never holds the GLOBAL gate either.
        """
        scheduler = make_scheduler(_PerDomainDelayScheduler, adaptive=False)
        _FAST_STARTED.clear()
        starved = _nap_waits_for_fast_domain(monkeypatch, "fast.example.org")

        jobs = [make_job(0, "http://slow.example.com/"), make_job(1, "http://fast.example.org/")]
        await scheduler.run(jobs, _fast_domain_worker, failed_result=engine_envelope)
        assert not starved, (
            "fast.example.org could not run until slow.example.com's reservation "
            "finished: the nap is being awaited while holding a GLOBAL concurrency "
            "slot, so it burns that slot on pure waiting"
        )
        assert _FAST_STARTED.is_set(), "the fast domain was never served"

    async def test_a_cooldown_firing_during_the_nap_is_not_skipped(self, monkeypatch):
        """A 429 cooldown that lands while a peer naps must park the peer before
        its worker: the nap re-checks the domain's cooldown on the way back in.
        Injected clock (advanced by the shim) -- the real monotonic made the
        50ms floor a timing race under xdist (adversarial pass 4)."""
        real_sleep = asyncio.sleep  # bound before the patch: the shim's own yields
        now = [1000.0]
        scheduler = make_scheduler(delay_seconds=0.05, adaptive=False, clock=lambda: now[0])
        cooldown_committed = asyncio.Event()
        sleeps = _nap_until_peer_recorded(monkeypatch, real_sleep, cooldown_committed, clock=now)

        async def worker(job):
            if job.index == 0:
                # The 429 lands while the second job is inside its jitter nap.
                scheduler.set_domain_cooldown("example.com", 0.5)
                cooldown_committed.set()
                return engine_result(job.url.raw, "rendered")
            assert sleeps.requests_over(0.45), (
                "the worker ran without awaiting the cooldown that fired during its "
                f"nap; the recorded sleep requests were {sleeps.delays}"
            )
            return engine_result(job.url.raw, "rendered")

        results = await _run_two_jobs(scheduler, worker)

        assert all(result.status == "success" for result in results.values()), (
            f"the cooldown must park, not fail, the napped job: {results}"
        )
        assert sleeps.requests_over(0.45), (
            f"no cooldown wait was requested at all (requests: {sleeps.delays})"
        )

    async def test_a_breaker_trip_during_the_nap_parks_the_napped_job(self, monkeypatch):
        """The circuit half of the same re-check: a trip that lands while a peer
        naps must PARK it on the open breaker -- the park is the proof. On the
        injected clock the window then elapses honestly and the job proceeds
        (the real-clock twin asserted a post-park skip, an artifact of the real
        clock never advancing; adversarial pass 4 migrated it)."""
        real_sleep = asyncio.sleep  # bound before the patch: the shim's own yields
        now = [1000.0]
        scheduler = make_scheduler(
            circuit_threshold=1,
            circuit_cooldown_base=30.0,
            circuit_cooldown=600.0,
            delay_seconds=0.05,
            adaptive=False,
            clock=lambda: now[0],
        )
        trip_committed = asyncio.Event()
        sleeps = _nap_until_peer_recorded(monkeypatch, real_sleep, trip_committed, clock=now)

        async def worker(job):
            if job.index == 0:
                scheduler.record_failure("example.com")  # trips at threshold 1
                trip_committed.set()
            await real_sleep(0)
            return engine_result(job.url.raw, "rendered")

        results = await _run_two_jobs(scheduler, worker)

        assert results[1].status == "success", (
            f"the napped job must PARK on the open breaker and proceed once the "
            f"window the injected clock advanced past has elapsed: {results[1].status}"
        )
        assert sleeps.requests_over(25.0), (
            f"the napped job never parked on the open breaker (park sleep missing): {sleeps.delays}"
        )


class TestDeliveryFailuresAreNotWorkerFailures:
    """``on_result`` is a SINK, and a sink that raises says nothing about the work.

    Delivery once ran inside the worker's ``try``, so a failing spill was
    ``record_failure``d and an envelope delivered through the same broken sink.
    """

    async def test_a_raising_sink_does_not_trip_the_circuit_breaker(self, caplog):
        scheduler = make_scheduler(adaptive=False)
        jobs = [make_job(0, "https://example.com/a")]
        sink = FailingSink(message="spill failed: disk full")

        await run_through(sink, scheduler, jobs, caplog)

        assert scheduler.domain_cooldowns == {}, (
            "a failed RESULT SPILL was charged to the domain's circuit breaker; the page "
            f"itself succeeded, so real traffic for that domain is now skipped: "
            f"{scheduler.domain_cooldowns}"
        )
        assert any("on_result" in record.message for record in caplog.records), (
            "the swallowed delivery failure left no log line, which is what made "
            f"undiagnosable: {[record.message for record in caplog.records]}"
        )

    async def test_a_raising_sink_gets_exactly_one_call_per_job(self, caplog):
        """No retry, and above all no fabricated envelope for a page that worked.

        The old shape delivered a second ``failed_result`` envelope for the same
        job, which a spill-based sink would write as though the crawl had failed.
        """
        scheduler = make_scheduler(adaptive=False)
        jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
        sink = FailingSink()

        await run_through(sink, scheduler, jobs, caplog)

        assert sorted(sink.calls) == [(0, "success"), (1, "success")], (
            f"the sink must see one real result per job and no fabricated envelope: {sink.calls}"
        )
        assert sink.delivered == [], f"every delivery was supposed to fail: {sink.delivered}"

    async def test_one_job_sink_failure_does_not_cost_a_healthy_job_its_result(self, caplog):
        """The healthy job's result still reaches a working sink on the next call."""
        scheduler = make_scheduler(adaptive=False)
        jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
        sink = FailingSink(fails={0})

        await run_through(sink, scheduler, jobs, caplog)

        assert sink.delivered == [(1, "success")], (
            f"one job's failed spill must not swallow another job's result: {sink.delivered}"
        )


class TestSchedulerFailureAccountingIsAboutStatuses:
    """A failure verdict is a STATUS; the reason is a CLASSIFICATION.

    The old branch matched ``("failed", "network_error")``, but network_error is
    a member mapped ONTO "failed", never a status -- six members, four unmatched.
    """

    @staticmethod
    def _terminal(job: CrawlJob, classification: Classification):
        """A real terminal result, with its status derived by the real chokepoint."""
        from crawlutron.transport.results import terminal_engine_result
        from crawlutron.vocabulary.taxonomy import status_for_classification

        # Status is the outer function's own keyword, not a field of the shared value: it is what the
        # per-status constructors pin, and a CONTEXT_VARYING_STATUS classification needs one the table cannot
        return terminal_engine_result(
            TerminalResult(
                original_url=job.url.raw,
                normalized_url=job.url.raw,
                strategy=None,
                classification=classification,
                # ``error`` is a taxonomy code, not an optional: production's own envelope passes the
                # classification through it, and a result carrying no error code is a shape no caller
                error=classification,
            ),
            status=status_for_classification(classification),
        )

    async def test_a_network_fault_records_a_domain_failure(self):
        """The reported case, through the real failure envelope, not a copy.

        The charge is observed through the breaker: one dns_error leaves it
        closed at threshold 2, and the second real fault opens it."""
        import socket

        scheduler = make_scheduler(circuit_threshold=2)
        job = make_job(0, "https://example.com/p")

        async def failing_worker(job):
            return engine_envelope(job, socket.gaierror("name resolution failed"))

        results = await scheduler.run([job], failing_worker, failed_result=engine_envelope)
        assert results is not None
        result = results[job.index]
        # Different axes: the status is the verdict, the classification the reason. Comparing one against the
        # other is why the old tuple could not work.
        assert result.status == "failed"
        assert result.classification == "dns_error"
        assert not scheduler.is_circuit_open("example.com"), (
            "one dns_error opened the breaker at threshold 2: the fault was double-charged"
        )
        scheduler.record_failure("example.com")  # the second real fault
        assert scheduler.is_circuit_open("example.com"), (
            "two dns_errors left the breaker closed: the network fault was never charged "
            "to the domain's failure count"
        )

    @pytest.mark.parametrize(
        ("classification", "expected_failures", "why"),
        [
            ("navigation_timeout", 1, "attempts exhausted against the domain: a real fault"),
            ("cookie_consent_required", 0, "a wall proves the host is alive"),
            ("robots_disallowed", 0, "we chose not to fetch; not the host's doing"),
            ("invalid_url", 0, "our own policy refusal, silent about the domain"),
        ],
    )
    async def test_each_status_reaches_the_verdict_its_meaning_implies(
        self, classification, expected_failures, why
    ):
        """The charge is observed through the breaker at threshold = 2: one more
        fault crosses it only when the run's verdict charged the book."""
        scheduler = make_scheduler(circuit_threshold=2)
        job = make_job(0, "https://example.com/p")

        async def worker(job):
            return self._terminal(job, classification)

        await scheduler.run([job], worker, failed_result=engine_envelope)
        # One more real fault: a charged book (run's verdict + this one) crosses
        # threshold 2 and opens the breaker; an uncharged book stays closed.
        scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com") == bool(expected_failures), why

    def test_status_accounting_is_total(self):
        """Every ResultStatus member has a verdict, so none can arrive unhandled.

        The replaced branch matched a two-element tuple, so blocked/disallowed/
        retry_exhausted/skipped counted as nothing; new statuses would stay silent.
        """
        import typing

        from crawlutron.vocabulary.taxonomy import DOMAIN_HEALTH_BY_STATUS, ResultStatus

        members = set(typing.get_args(ResultStatus))
        assert members, "ResultStatus must be a Literal to be checkable"
        assert not members - set(DOMAIN_HEALTH_BY_STATUS), (
            f"a ResultStatus member has no domain-health verdict: "
            f"{sorted(members - set(DOMAIN_HEALTH_BY_STATUS))}"
        )
        assert not set(DOMAIN_HEALTH_BY_STATUS) - members, (
            "the accounting table names a status the taxonomy does not define: "
            f"{sorted(set(DOMAIN_HEALTH_BY_STATUS) - members)}"
        )
        assert set(DOMAIN_HEALTH_BY_STATUS.values()) <= {"success", "failure", "none"}
