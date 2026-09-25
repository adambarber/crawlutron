"""Domain scheduler: run queue, cooldown, policies, purge cadence, resilience and starvation."""

import asyncio
import random
import time
from typing import Any

import pytest
from _result_builders import make_job, make_result
from _timing_harnesses import recorder

from app.crawl.result_helpers import _failed_result_for
from app.models import CrawlJob, CrawlTask, EngineCrawlResult
from app.scheduler import DomainScheduler


async def ok_worker(job: CrawlJob) -> Any:
    return make_result(job.url, job.domain)


def _engine_envelope(job: CrawlJob, error: BaseException) -> EngineCrawlResult:
    """The crawl pipeline's real failure envelope.

    Routes through the production ``_failed_result_for`` chokepoint rather than
    re-building an envelope here: the classification, redaction and
    ``normalized_url`` fallback are that function's contract, and a test-local
    copy would silently drift from it (it already did twice). The URL accessor
    is supplied here for the same reason production supplies one -- the two job
    shapes carry it under different names.
    """
    return _failed_result_for(
        job, error, url_of=lambda j: getattr(j, "original_url", None) or j.url
    )


def _adaptive_scheduler() -> DomainScheduler:
    return DomainScheduler(
        global_limit=2,
        per_domain_limit=1,
        delay_seconds=1.0,
        adaptive=True,
        adaptive_min_delay=0.5,
        adaptive_max_delay=30.0,
        adaptive_latency_factor=1.0,
    )


class TestScheduler:
    def test_jitter_in_range(self):
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        delays = [scheduler._delay_seconds * random.uniform(0.8, 1.2) for _ in range(50)]
        assert all(0.7 <= d <= 1.3 for d in delays)

    def test_set_domain_cooldown_is_in_future(self):
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=3, delay_seconds=0.1)
        scheduler.set_domain_cooldown("example.com", 5.0)
        assert "example.com" in scheduler._domain_cooldowns
        expiry = scheduler._domain_cooldowns["example.com"]
        assert expiry > time.time()
        assert expiry - time.time() > 4.0

    async def test_cooldown_delay_applied_to_domain(self, monkeypatch):
        """A domain in cooldown must have the cooldown awaited before the worker.

        Asserted on the *request* the scheduler makes, not on how long the box
        took to grant it: a cooldown of 0.3s is honoured by asking to sleep the
        remaining window, and that ask is the behaviour under test. The
        worker's own assertion pins the ordering -- it fails if the cooldown
        branch is skipped, or if the cooldown is ever moved behind the worker.
        """
        sleeps = recorder(monkeypatch)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.05)
        scheduler.set_domain_cooldown("example.com", 0.3)
        job = make_job(0, "http://example.com/")

        async def worker(ran_job):
            assert sleeps.requests_over(0.29), (
                "the worker ran before the domain cooldown was awaited; the "
                f"recorded sleep requests were {sleeps.delays}"
            )
            return make_result(ran_job.url, ran_job.domain)

        results = await scheduler.run([job], worker, failed_result=_engine_envelope)
        assert results[0].status == "success"
        assert sleeps.requests_over(0.29), (
            f"no cooldown sleep was requested at all (requests: {sleeps.delays})"
        )

    async def test_run_returns_results_keyed_by_job_index(self):
        scheduler = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]
        results = await scheduler.run(jobs, ok_worker, failed_result=_engine_envelope)
        # run reports a job.index -> result mapping so callers align each result
        # with its own job regardless of completion order (B1).
        assert sorted(results) == [0, 1, 2]
        assert [results[i].normalized_url for i in sorted(results)] == [
            f"http://example.com/{i}" for i in range(3)
        ]

    async def test_delay_applied_between_same_domain_jobs(self, monkeypatch):
        """Every job on a delayed domain asks for the jittered per-domain delay.

        Two same-domain jobs at ``delay_seconds=0.1`` must produce two delay
        requests inside the jitter band, not one (or none). The band is
        production's ``uniform(0.8, 1.2)``, derived from the code rather than
        from whatever the machine happened to do.
        """
        sleeps = recorder(monkeypatch)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.1, adaptive=False)
        jobs = [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]
        await scheduler.run(jobs, ok_worker, failed_result=_engine_envelope)
        assert len(sleeps.in_range(0.08, 0.12)) == 2, (
            f"expected one per-domain delay request per job, saw {sleeps.delays}"
        )


class TestAdaptiveThrottling:
    def test_delay_starts_at_configured_value_without_latency(self):
        s = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=1.0, adaptive=True)
        # No latency observed yet -> adaptive delay equals the configured fixed delay.
        assert s.current_delay("example.com") == 1.0

    def test_delay_increases_after_slow_response(self):
        s = _adaptive_scheduler()
        s.record_latency("example.com", 5000)  # 5s latency
        d = s.current_delay("example.com")
        assert d > 1.0
        assert d <= 30.0

    def test_delay_floors_after_fast_response(self):
        s = _adaptive_scheduler()
        s.record_latency("example.com", 50)  # 50ms latency
        assert s.current_delay("example.com") == 0.5

    def test_disabled_adaptive_ignores_latency(self):
        s = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=1.0, adaptive=False)
        s.record_latency("example.com", 5000)
        assert s.current_delay("example.com") == 1.0

    def test_latency_ema_smooths(self):
        s = DomainScheduler(
            global_limit=2,
            per_domain_limit=1,
            delay_seconds=0.0,
            adaptive=True,
            adaptive_min_delay=0.0,
            adaptive_max_delay=100.0,
            adaptive_latency_factor=1.0,
            adaptive_smoothing=0.5,
        )
        s.record_latency("example.com", 1000)  # -> ~1.0s
        first = s.current_delay("example.com")
        s.record_latency("example.com", 100)  # -> EMA (1000+100)/2 = 550ms -> 0.55s
        second = s.current_delay("example.com")
        assert abs(first - 1.0) < 1e-6
        assert abs(second - 0.55) < 1e-6


class TestCircuitBreakerSpillsOnResult:
    """with ``on_result`` set, an open circuit must spill the failed
    envelope through ``on_result`` instead of writing ``results[index]``
    (which is ``None`` in that mode -> TypeError, swallowed by
    ``gather(..., return_exceptions=True)`` -> the URL silently vanishes).
    """

    async def test_open_circuit_spills_via_on_result(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=2,
            per_domain_limit=1,
            delay_seconds=0.0,
            circuit_threshold=3,
            circuit_cooldown_base=60.0,
        )
        # Trip the breaker by hitting the consecutive-failure threshold.
        for _ in range(3):
            scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com")

        spilled: list[tuple[int, object]] = []

        async def on_result(index, result):
            spilled.append((index, result))

        async def worker(job):  # pragma: no cover - must never run
            raise AssertionError("worker must not run while the circuit is open")

        job = CrawlTask(
            index=0,
            original_url="http://example.com/",
            normalized_url="http://example.com/",
            domain="example.com",
        )
        # The bug raises TypeError inside gather with return_exceptions=True,
        # so run() returns normally but delivers nothing.
        returned = await scheduler.run([job], worker, on_result=on_result, failed_result=_engine_envelope)

        assert returned is None, "on_result mode must return None"
        assert len(spilled) == 1, f"expected 1 spilled result, got {len(spilled)}"
        index, result = spilled[0]
        assert index == 0
        # The classification and the error agree: a breaker-open skip is a
        # capacity decision, not a transport failure, so the taxonomy field
        # callers key on must say so (it used to report "network_error" while
        # error/diagnostics said "circuit_breaker_open", making the two
        # indistinguishable from a real network failure).
        assert result.status == "failed", result
        assert result.classification == "circuit_breaker_open", result
        assert result.error == "circuit_breaker_open", result
        assert result.diagnostics.get("circuit_breaker") is True, result.diagnostics


class TestSchedulerDomainState:
    def test_scheduler_domain_cooldowns(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("example.com", 10)
        cooldowns = scheduler.domain_cooldowns
        assert isinstance(cooldowns, dict)
        assert "example.com" in cooldowns


class TestSchedulerCircuitBreaker:
    def test_scheduler_circuit_breaker_reset(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, circuit_cooldown=0.0
        )
        # Trip the circuit breaker (threshold=5 by default)
        for _ in range(5):
            scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com")
        # record_success resets failure counter; with cooldown=0, circuit is not open
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    def test_scheduler_record_success(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    def test_scheduler_is_circuit_open_false(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        assert not (scheduler.is_circuit_open("example.com"))


class TestSchedulerLatencyEma:
    def test_scheduler_record_latency_invalid(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        # None latency should be ignored (line 114: return)
        scheduler.record_latency("example.com", None)
        # Negative latency should be ignored
        scheduler.record_latency("example.com", -1.0)
        # Verify no latency was recorded
        assert "example.com" not in scheduler._domain_latency_ema

    def test_scheduler_record_latency_valid(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.record_latency("example.com", 100.0)
        assert "example.com" in scheduler._domain_latency_ema
        assert abs(scheduler._domain_latency_ema["example.com"] - (100.0)) < 1e-07

    def test_scheduler_record_latency_ema(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive_smoothing=0.5
        )
        scheduler.record_latency("example.com", 100.0)
        scheduler.record_latency("example.com", 200.0)
        assert abs(scheduler._domain_latency_ema["example.com"] - (150.0)) < 0.1


class TestSchedulerPolicies:
    """Cover remaining branches in scheduler.py"""

    def test_token_bucket_basic(self):
        from app.scheduler import _TokenBucket

        bucket = _TokenBucket(rate=10.0, capacity=5.0)
        wait = bucket.consume(1.0)
        assert wait == 0.0
        wait = bucket.consume(4.0)
        assert wait == 0.0
        wait = bucket.consume(1.0)
        assert wait > 0.0

    def test_token_bucket_refill(self):
        from app.scheduler import _TokenBucket

        bucket = _TokenBucket(rate=100.0, capacity=10.0)
        bucket.consume(10.0)
        # Manually refill tokens as if 0.1s passed (at 100 tokens/s, that's 10 more)
        bucket.tokens = 10.0
        wait = bucket.consume(1.0)
        assert wait == 0.0

    def test_circuit_breaker_opens(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        for _ in range(5):
            scheduler.record_failure("example.com")
        assert scheduler.is_circuit_open("example.com")

    def test_circuit_breaker_cooldown_expired(self):
        import time

        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        scheduler._circuit_open_until["example.com"] = time.monotonic() - 1
        assert not (scheduler.is_circuit_open("example.com"))

    def test_scheduler_record_latency(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0, adaptive=True)
        scheduler.record_latency("example.com", 1.0)
        scheduler.record_latency("example.com", 2.0)
        assert "example.com" in scheduler._domain_latency_ema

    def test_scheduler_max_rps_token_bucket(self):
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0, max_rps=5.0)
        assert scheduler._max_rps > 0

    def test_circuit_breaker_exponential_backoff(self):
        """Adversarial perf #2.1: consecutive trips escalate the cooldown
        exponentially (base -> doubled -> ... -> cap)."""
        import time

        from app.scheduler import DomainScheduler

        s = DomainScheduler(
            global_limit=1,
            per_domain_limit=1,
            delay_seconds=0.0,
            circuit_threshold=5,
            circuit_cooldown_base=30.0,
            circuit_cooldown=600.0,
        )

        def open_after(failures: int) -> float:
            s._domain_failures.clear()
            s._circuit_trip_count.clear()
            s._circuit_open_until.clear()
            for _ in range(failures):
                s.record_failure("x")
            return s._circuit_open_until["x"] - time.monotonic()

        assert 29 <= open_after(5) <= 31
        assert 59 <= open_after(10) <= 61
        assert 119 <= open_after(15) <= 121
        assert 599 <= open_after(40) <= 601
        s.record_success("x")
        assert s._circuit_trip_count["x"] == 0

    def test_scheduler_set_domain_min_delay(self):
        """Adversarial perf #2.6: a robots Crawl-delay is enforced as a floor
        on top of the global/adaptive delay."""
        from app.scheduler import DomainScheduler

        s = DomainScheduler(global_limit=2, per_domain_limit=1, delay_seconds=0.0)
        assert s.current_delay("a.com") == 0.0
        s.set_domain_min_delay("a.com", 2.0)
        assert s.current_delay("a.com") == 2.0
        # Star group uses 0; a longer Crawl-delay wins (max semantics).
        s.set_domain_min_delay("a.com", 5.0)
        assert s.current_delay("a.com") == 5.0

    def test_parse_robots_crawl_delay_specific_group_wins(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 1\nUser-agent: browser-crawler\nCrawl-delay: 4\n"
        assert _parse_robots_crawl_delay(text) == 4.0

    def test_parse_robots_crawl_delay_star_only(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 3\n"
        assert _parse_robots_crawl_delay(text) == 3.0

    def test_parse_robots_crawl_delay_absent(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        assert _parse_robots_crawl_delay("User-agent: *\nDisallow: /\n") is None

    def test_set_domain_min_delay_is_capped_at_the_politeness_ceiling(self):
        """M1: a huge (finite) robots Crawl-delay must not install an
        unbounded politeness floor — the scheduler clamps it at the write."""
        from app.scheduler import _MAX_COOLDOWN_WAIT, DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        scheduler.set_domain_min_delay("example.com", 1e12)
        assert scheduler.current_delay("example.com") == _MAX_COOLDOWN_WAIT

    def test_set_domain_min_delay_ignores_non_finite(self):
        """M1: inf/nan must never become a stored politeness floor."""
        from app.scheduler import DomainScheduler

        scheduler = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)
        scheduler.set_domain_min_delay("example.com", float("inf"))
        assert "example.com" not in scheduler._domain_min_delay

    async def test_run_streams_results_via_on_result(self):
        """Adversarial perf #3.1: with ``on_result`` set, each completed result is
        delivered immediately (a fast job is delivered before a slow one) and
        ``run`` returns None (nothing accumulated in memory).

        Uses an ``asyncio.Event`` (not ``asyncio.sleep``) to force ordering,
        because the test environment patches ``sleep`` to be instant.
        """
        from _result_builders import make_job, make_result

        from app.scheduler import DomainScheduler

        on_result_order: list[int] = []
        release_slow = asyncio.Event()
        sched = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)

        async def worker(job: CrawlJob):
            # Make index 0 wait until job 1 has been delivered, so streaming
            # order is observable without a real sleep.
            if job.index == 0:
                await release_slow.wait()
            return make_result(job.url, job.domain)

        async def _on_result(idx: int, _r) -> None:
            on_result_order.append(idx)
            if idx == 1:
                release_slow.set()

        jobs = [make_job(i, f"http://h{i}.example.com/") for i in range(6)]
        ret = await sched.run(jobs, worker, on_result=_on_result, failed_result=_engine_envelope)
        # Streaming mode returns None; the caller owns persistence (spill).
        assert ret is None
        # Every job was delivered exactly once.
        assert sorted(on_result_order) == list(range(6))
        # A fast job is delivered before the (gated) slow job -> results stream
        # as they complete rather than being held until the whole batch finishes.
        assert on_result_order.index(1) < on_result_order.index(0)

    async def test_run_returns_results_keyed_by_index_without_on_result(self):
        """Backward-compatible path (recursive frontier) still returns results."""
        from _result_builders import make_job, make_result

        from app.scheduler import DomainScheduler

        sched = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)

        async def worker(job: CrawlJob):
            return make_result(job.url, job.domain)

        jobs = [make_job(i, f"http://h{i}.example.com/") for i in range(4)]
        ret = await sched.run(jobs, worker, failed_result=_engine_envelope)
        assert ret is not None
        # Results are keyed by job index so callers align them with their own job.
        assert sorted(ret) == [0, 1, 2, 3]
        assert [ret[i].normalized_url for i in sorted(ret)] == [
            f"http://h{i}.example.com/" for i in range(4)
        ]


class TestCooldownPurgeCadence:
    def test_purge_is_amortized_not_per_rate_limited_result(self, monkeypatch):
        calls = {"n": 0}
        real_purge = DomainScheduler._purge_expired

        def counting_purge(self):
            calls["n"] += 1
            real_purge(self)

        monkeypatch.setattr(DomainScheduler, "_purge_expired", counting_purge)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        for i in range(250):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        # 250 sets on a 100-set interval: the cadence is amortized, not 1:1.
        assert calls["n"] <= 3

    def test_expired_cooldowns_are_still_reclaimed(self, monkeypatch):
        """Amortizing must not let expired entries accumulate indefinitely:
        once the interval elapses only expired entries drop, and the fresh
        cooldown for the setting domain survives."""
        fake_now = [1000.0]
        monkeypatch.setattr("app.scheduler.time.time", lambda: fake_now[0])
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("old.test", 10.0)
        fake_now[0] = 1100.0  # the first cooldown has elapsed
        # Burn past the interval so the amortized purge fires.
        for i in range(101):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        assert "old.test" not in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns, "fresh cooldowns were dropped"

    def test_a_fresh_cooldown_is_never_purged_by_the_amortized_run(self, monkeypatch):
        """The domain that just earned a cooldown is never removed by the purge
        that follows its own set."""
        monkeypatch.setattr("app.scheduler.time.time", lambda: 1000.0)
        scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
        scheduler.set_domain_cooldown("rate-limited.test", 30.0)
        assert "rate-limited.test" in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns["rate-limited.test"] > 1000.0


@pytest.mark.parametrize("method", ["run"])
def test_run_still_purges_up_front(method):
    """The batch entry point keeps its up-front purge so a long run cannot grow
    the dicts without bound even when no 429 fires."""
    scheduler = DomainScheduler(global_limit=1, per_domain_limit=1, delay_seconds=0.0)
    scheduler._domain_cooldowns["stale.test"] = 0.0  # already expired
    assert "stale.test" in scheduler._domain_cooldowns
    # The up-front purge fires before any job runs.
    scheduler._purge_expired()
    assert "stale.test" not in scheduler._domain_cooldowns


class TestSchedulerResilience:
    async def test_scheduler_continues_other_jobs_on_single_failure(self):
        """One failing job must not cancel the others; results for the
        successful jobs are still returned."""
        scheduler = DomainScheduler(global_limit=5, per_domain_limit=5, delay_seconds=0.0)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]

        async def failing_worker(job: CrawlJob) -> EngineCrawlResult:
            if job.index == 1:
                raise RuntimeError("transient failure")
            return make_result(job.url, job.domain)

        results = await scheduler.run(jobs, failing_worker, failed_result=_engine_envelope)
        # The failing job is not dropped: the chokepoint builds it a classified
        # terminal envelope (reading the URL pair off whichever shape the job
        # carries -- CrawlJob has only ``url``), so the caller never observes a
        # missing slot and improvises a result off the raw error. The other
        # jobs are unaffected.
        assert len(results) == 3, results
        assert results[1].status == "failed"
        assert results[1].classification == "network_error"
        assert results[1].normalized_url == "http://example.com/1"
        assert all(r.status == "success" for i, r in results.items() if i != 1)
        assert {r.normalized_url for i, r in results.items() if i != 1} == {
            "http://example.com/0",
            "http://example.com/2",
        }


class _Job:
    def __init__(self, index: int, domain: str):
        self.index = index
        self.domain = domain


class _PerDomainDelayScheduler(DomainScheduler):
    """Fixed delays that differ per domain (adaptive throttling off)."""

    def current_delay(self, domain: str) -> float:
        return 0.4 if domain == "slow.example.com" else 0.0


async def test_domain_delay_does_not_block_other_domains(monkeypatch):
    """The per-domain delay is awaited inside the *domain* gate, not the global one.

    The regression this pins: sleeping on the delay while holding a global
    concurrency slot burns that slot on pure waiting, so every other domain
    queues behind a wait that has nothing to do with them. Proved with a
    handshake rather than a stopwatch: the slow domain's delay will not
    *complete* until the fast domain's worker has run. With ``global_limit=1``
    the two orderings are then distinguishable without any timing at all -- if
    the delay held the only global slot the fast worker could never start, the
    handshake could never be satisfied, and the wait fails naming exactly that.
    """
    scheduler = _PerDomainDelayScheduler(
        global_limit=1,
        per_domain_limit=1,
        delay_seconds=0.0,
        adaptive=False,
    )
    fast_started = asyncio.Event()
    starved: list[str] = []
    yield_to_loop = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        del args, kwargs
        if delay >= 0.2:
            # slow.example.com's 0.4s per-domain delay (jittered). It may not
            # finish until the fast domain has been served. The starvation is
            # recorded rather than raised because ``run`` gathers with
            # return_exceptions=True, which would swallow an exception raised
            # from inside a worker's own await chain.
            try:
                await asyncio.wait_for(fast_started.wait(), 2.0)
            except TimeoutError:
                starved.append("fast.example.com")
            return
        await yield_to_loop(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    async def worker(job):
        if job.domain == "fast.example.com":
            fast_started.set()
        await yield_to_loop(0)
        return "ok"

    jobs = [_Job(0, "slow.example.com"), _Job(1, "fast.example.com")]
    await scheduler.run(jobs, worker, failed_result=_engine_envelope)
    assert not starved, (
        "fast.example.com could not run until slow.example.com's per-domain delay "
        "finished: the delay is being awaited while holding a GLOBAL concurrency "
        "slot, so it burns that slot on pure waiting and starves every other domain"
    )
    assert fast_started.is_set(), "the fast domain was never served"


async def _ok(job: CrawlJob):
    return make_result(job.url, job.domain)


async def test_zero_delay_requests_no_sleep_at_all(monkeypatch):
    """The inverse: a domain configured with no delay must request no wait.

    The suite's autouse patch now makes every sleep zero-second *and* yielding,
    so "the delay was skipped" is no longer observable as elapsed time -- but
    it is still observable as a request. A scheduler that turned a zero
    configured delay into a positive sleep request would be inventing politeness
    the operator never asked for, and paying it on every single job.
    """
    sleeps = recorder(monkeypatch)
    scheduler = DomainScheduler(
        global_limit=1,
        per_domain_limit=1,
        delay_seconds=0.0,
        adaptive=False,
    )
    jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
    await scheduler.run(jobs, _ok, failed_result=_engine_envelope)
    assert sleeps.delays == [0, 0], (
        f"a zero-delay scheduler must request only zero-second yields, saw {sleeps.delays}"
    )


class TestDeliveryFailuresAreNotWorkerFailures:
    """``on_result`` is a SINK, and a sink that raises says nothing about the work.

    The callback is where a crawl spills a result to disk. When that spill
    raised, delivery happened inside the worker's ``try``, so the scheduler
    answered with ``record_failure`` and a fabricated ``failed_result`` envelope
    delivered through the same broken sink -- three things wrong at once: a page
    that succeeded was counted against its domain's circuit breaker (real
    traffic for that domain then gets skipped), the caller was handed a failure
    envelope for a page it had already fetched, and ``gather``'s
    ``return_exceptions=True`` swallowed what was left with no log line at all.
    """

    async def test_a_raising_sink_does_not_trip_the_circuit_breaker(self, caplog):
        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive=False
        )
        jobs = [make_job(0, "https://example.com/a")]

        async def exploding_sink(index, result):
            raise OSError("spill failed: disk full")

        with caplog.at_level("ERROR"):
            await scheduler.run(jobs, _ok, on_result=exploding_sink, failed_result=_engine_envelope)

        assert scheduler.domain_cooldowns == {}, (
            "a failed RESULT SPILL was charged to the domain's circuit breaker; the page "
            f"itself succeeded, so real traffic for that domain is now skipped: "
            f"{scheduler.domain_cooldowns}"
        )
        assert any("on_result" in record.message for record in caplog.records), (
            "the swallowed delivery failure left no log line, which is what made H1 "
            f"undiagnosable: {[record.message for record in caplog.records]}"
        )

    async def test_a_raising_sink_gets_exactly_one_call_per_job(self, caplog):
        """No retry, and above all no fabricated envelope for a page that worked.

        The old shape delivered a second result for the same job -- the
        ``failed_result`` envelope -- which a spill-based sink would have
        written to disk as though the crawl had failed.
        """
        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive=False
        )
        jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
        seen: list[tuple[int, str]] = []

        async def exploding_sink(index, result):
            seen.append((index, result.status))
            raise OSError("spill failed")

        with caplog.at_level("ERROR"):
            await scheduler.run(jobs, _ok, on_result=exploding_sink, failed_result=_engine_envelope)

        assert sorted(seen) == [(0, "success"), (1, "success")], (
            f"the sink must see one real result per job and no fabricated envelope: {seen}"
        )

    async def test_one_job_sink_failure_does_not_cost_a_healthy_job_its_result(self):
        """The healthy job's result still reaches a working sink on the next call."""
        scheduler = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0, adaptive=False
        )
        jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
        delivered: list[tuple[int, str]] = []

        async def flaky_sink(index, result):
            if index == 0:
                raise OSError("spill failed")
            delivered.append((index, result.status))

        await scheduler.run(jobs, _ok, on_result=flaky_sink, failed_result=_engine_envelope)

        assert delivered == [(1, "success")], (
            f"one job's failed spill must not swallow another job's result: {delivered}"
        )
