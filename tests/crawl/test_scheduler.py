"""Domain scheduler: run queue, cooldown, policies, purge cadence, resilience and starvation."""

import asyncio
import time

import pytest

from app.crawl.result_helpers import TerminalResult
from app.models.crawl_job import CrawlJob
from app.models.crawl_task import CrawlTask
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.taxonomy import Classification
from app.scheduler import _COOLDOWN_PURGE_INTERVAL, DomainScheduler, jittered_delay
from tests._policy_factories import canonical
from tests._result_builders import make_job, make_result
from tests._scheduler_doubles import (
    FailingSink,
    engine_envelope,
    make_scheduler,
    ok_worker,
    run_through,
    trip_circuit,
)
from tests._timing_harnesses import recorder


def _adaptive_scheduler() -> DomainScheduler:
    return make_scheduler(
        global_limit=2,
        delay_seconds=1.0,
        adaptive=True,
        adaptive_min_delay=0.5,
        adaptive_max_delay=30.0,
        adaptive_latency_factor=1.0,
    )


class TestScheduler:
    def test_jitter_in_range(self):
        scheduler = make_scheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        base = scheduler._delay_seconds
        delays = [jittered_delay(base) for _ in range(50)]
        assert all(0.7 <= d <= 1.3 for d in delays), delays
        # A zero base must not sleep at all, whatever the jitter draws. This
        # branch is the reason the band is not applied unconditionally.
        assert jittered_delay(0.0) == 0.0

    def test_set_domain_cooldown_is_in_future(self):
        scheduler = make_scheduler(global_limit=5, per_domain_limit=3, delay_seconds=0.1)
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

        The 90%-of-window floor (0.27 of 0.3) is not the whole window on
        purpose: the remaining window shrinks by whatever the test itself
        spends between ``set_domain_cooldown`` and ``run`` -- admitting the
        job's URL, building the scheduler's gates -- so an exact-0.29 floor
        flakes on a loaded box while saying nothing more about the scheduler.
        """
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(delay_seconds=0.05)
        scheduler.set_domain_cooldown("example.com", 0.3)
        job = make_job(0, "http://example.com/")

        async def worker(ran_job: CrawlJob) -> EngineCrawlResult:
            assert sleeps.requests_over(0.27), (
                "the worker ran before the domain cooldown was awaited; the "
                f"recorded sleep requests were {sleeps.delays}"
            )
            return make_result(ran_job.url.raw, ran_job.domain)

        # The delivery channel decides the return: with no sink, ``run`` hands
        # back the index-keyed mapping (the docstring's "otherwise" branch). The
        # guard is the contract, stated where the call that relies on it is made.
        results = await scheduler.run([job], worker, failed_result=engine_envelope)
        assert results is not None
        assert results[0].status == "success"
        assert sleeps.requests_over(0.27), (
            f"no cooldown sleep was requested at all (requests: {sleeps.delays})"
        )

    async def test_run_returns_results_keyed_by_job_index(self):
        scheduler = make_scheduler(global_limit=2)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]
        results = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert results is not None
        # Run reports a job.index -> result mapping so callers align each result
        # with its own job regardless of completion order.
        assert sorted(results) == [0, 1, 2]
        assert [results[i].normalized_url for i in sorted(results)] == [
            canonical(f"http://example.com/{i}") for i in range(3)
        ]

    async def test_delay_applied_between_same_domain_jobs(self, monkeypatch):
        """Every job on a delayed domain asks for the jittered per-domain delay.

        Two same-domain jobs at ``delay_seconds=0.1`` must produce two delay
        requests inside the jitter band, not one (or none). The band is
        production's ``uniform(0.8, 1.2)``, derived from the code rather than
        from whatever the machine happened to do.
        """
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(delay_seconds=0.1, adaptive=False)
        jobs = [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]
        await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert len(sleeps.in_range(0.08, 0.12)) == 2, (
            f"expected one per-domain delay request per job, saw {sleeps.delays}"
        )


class TestAdaptiveThrottling:
    def test_delay_starts_at_configured_value_without_latency(self):
        s = make_scheduler(global_limit=2, delay_seconds=1.0, adaptive=True)
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
        s = make_scheduler(global_limit=2, delay_seconds=1.0, adaptive=False)
        s.record_latency("example.com", 5000)
        assert s.current_delay("example.com") == 1.0

    def test_latency_ema_smooths(self):
        s = make_scheduler(
            global_limit=2,
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


class TestObservedLatencyCrossesPipelines:
    """Both result shapes must feed the estimator, through one attribute.

    The scheduler used to dig latency out of ``result.diagnostics["runtime"]``,
    and only ``EngineCrawlResult`` carries ``diagnostics``. Every /v1/render
    result is a ``CrawlResult``, which has ``elapsed_ms`` directly and no
    ``diagnostics`` at all -- so the getattr returned None and the render
    pipeline's latencies were silently dropped, leaving the adaptive estimator
    calibrated on crawl traffic alone.

    Both shapes are the real models, and the crawl one is built through the
    real ``build_engine_result`` chokepoint. A stand-in carrying a
    ``diagnostics`` key would have passed against the old code, which is the
    whole point: the defect was the attribute a shape does not have, so the
    fixture has to be the shape itself.
    """

    @staticmethod
    def _rendered_result(elapsed_ms: int):
        from app.crawl.result_helpers import terminal_attempt
        from app.models.crawl_result import CrawlResult

        return CrawlResult(
            url="https://example.com/x",
            attempt=terminal_attempt(
                strategy=None,
                status="success",
                classification="rendered",
                elapsed_ms=elapsed_ms,
                error=None,
            ),
            final_url="https://example.com/x",
            html=None,
            text=None,
            attempts=1,
            domain="example.com",
        )

    @staticmethod
    def _crawled_result(elapsed_ms: int):
        from app.crawl.result_helpers import build_engine_result
        from app.models.crawl_result import CrawlStrategyAttempt

        return build_engine_result(
            original_url="https://example.com/x",
            normalized_url="https://example.com/x",
            final_url="https://example.com/x",
            status="success",
            classification="rendered",
            attempt=CrawlStrategyAttempt(
                strategy="HTTP_DECLARED_CRAWLER",
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=elapsed_ms,
            ),
            http_status=200,
            error=None,
            diagnostics={"runtime": {"elapsed_ms": elapsed_ms}},
        )

    @pytest.mark.parametrize("build_result", [_rendered_result, _crawled_result], ids=["render", "crawl"])
    def test_latency_reaches_the_estimator_from_either_pipeline(self, build_result):
        s = _adaptive_scheduler()
        s._record_result_health("example.com", build_result(1000))
        assert abs(s.current_delay("example.com") - 1.0) < 1e-6

    @pytest.mark.parametrize("build_result", [_rendered_result, _crawled_result], ids=["render", "crawl"])
    def test_a_result_with_no_observation_leaves_the_delay_alone(self, build_result):
        s = _adaptive_scheduler()
        s._record_result_health("example.com", build_result(0))
        # Same baseline the sibling test asserts for an unlearned domain: the
        # configured fixed delay, unchanged because nothing was observed.
        assert s.current_delay("example.com") == 1.0


class TestCircuitBreakerSpillsOnResult:
    """With ``on_result`` set, an open circuit must spill the failed
    envelope through ``on_result`` instead of writing ``results[index]``
    (which is ``None`` in that mode -> TypeError, swallowed by
    ``gather(..., return_exceptions=True)`` -> the URL silently vanishes).
    """

    async def test_open_circuit_spills_via_on_result(self):
        scheduler = make_scheduler(
            global_limit=2,
            circuit_threshold=3,
            circuit_cooldown_base=60.0,
        )
        # Trip the breaker by hitting the consecutive-failure threshold.
        trip_circuit(scheduler, "example.com")
        assert scheduler.is_circuit_open("example.com")

        spilled: list[tuple[int, EngineCrawlResult]] = []

        async def on_result(index: int, result: EngineCrawlResult) -> None:
            spilled.append((index, result))

        async def worker(job: CrawlTask) -> EngineCrawlResult:  # pragma: no cover - must never run
            raise AssertionError("worker must not run while the circuit is open")

        job = CrawlTask(
            index=0,
            original_url="http://example.com/",
            normalized_url=canonical("http://example.com/"),
            domain="example.com",
        )
        # The bug raises TypeError inside gather with return_exceptions=True,
        # so run() returns normally but delivers nothing.
        # The delivery channel decides the return: with no sink, ``run`` hands
        # back the index-keyed mapping (the docstring's "otherwise" branch). The
        # guard is the contract, stated where the call that relies on it is made.
        returned = await scheduler.run([job], worker, on_result=on_result, failed_result=engine_envelope)

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
        scheduler = make_scheduler()
        scheduler.set_domain_cooldown("example.com", 10)
        cooldowns = scheduler.domain_cooldowns
        assert isinstance(cooldowns, dict)
        assert "example.com" in cooldowns


class TestSchedulerCircuitBreaker:
    def test_scheduler_circuit_breaker_reset(self):
        scheduler = make_scheduler(circuit_cooldown=0.0)
        trip_circuit(scheduler, "example.com")
        assert scheduler.is_circuit_open("example.com")
        # Record_success resets failure counter; with cooldown=0, circuit is not open
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    def test_scheduler_record_success(self):
        scheduler = make_scheduler()
        scheduler.record_success("example.com")
        assert scheduler._domain_failures.get("example.com", 0) == 0

    def test_scheduler_is_circuit_open_false(self):
        scheduler = make_scheduler()
        assert not (scheduler.is_circuit_open("example.com"))


class TestSchedulerLatencyEma:
    def test_scheduler_record_latency_invalid(self):
        scheduler = make_scheduler()
        # A negative latency is a broken measurement, not a fast one: recording
        # it would pull the EMA under every delay actually observed and let the
        # domain through faster than anything it has been timed at.
        scheduler.record_latency("example.com", -1.0)
        assert "example.com" not in scheduler._domain_latency_ema

    def test_scheduler_record_latency_valid(self):
        scheduler = make_scheduler()
        scheduler.record_latency("example.com", 100.0)
        assert "example.com" in scheduler._domain_latency_ema
        assert abs(scheduler._domain_latency_ema["example.com"] - (100.0)) < 1e-07

    def test_scheduler_record_latency_ema(self):
        scheduler = make_scheduler(adaptive_smoothing=0.5)
        scheduler.record_latency("example.com", 100.0)
        scheduler.record_latency("example.com", 200.0)
        assert abs(scheduler._domain_latency_ema["example.com"] - (150.0)) < 0.1


class TestSchedulerPolicies:
    """Cover remaining branches in scheduler.py"""

    def test_token_bucket_basic(self):
        from app.scheduler import _TokenBucket

        bucket = _TokenBucket(rate=10.0, capacity=5)
        wait = bucket.consume(1.0)
        assert wait == 0.0
        wait = bucket.consume(4.0)
        assert wait == 0.0
        wait = bucket.consume(1.0)
        assert wait > 0.0

    def test_token_bucket_refill(self):
        from app.scheduler import _TokenBucket

        bucket = _TokenBucket(rate=100.0, capacity=10)
        bucket.consume(10.0)
        # Manually refill tokens as if 0.1s passed (at 100 tokens/s, that's 10 more)
        bucket.tokens = 10.0
        wait = bucket.consume(1.0)
        assert wait == 0.0

    def test_circuit_breaker_opens(self):
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        trip_circuit(scheduler, "example.com")
        assert scheduler.is_circuit_open("example.com")

    def test_circuit_breaker_cooldown_expired(self):
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler._circuit_open_until["example.com"] = time.monotonic() - 1
        assert not (scheduler.is_circuit_open("example.com"))

    def test_scheduler_record_latency(self):
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, adaptive=True)
        scheduler.record_latency("example.com", 1.0)
        scheduler.record_latency("example.com", 2.0)
        assert "example.com" in scheduler._domain_latency_ema

    def test_scheduler_max_rps_token_bucket(self):
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, max_rps=5.0)
        assert scheduler._max_rps > 0

    def test_circuit_breaker_exponential_backoff(self):
        """Consecutive trips escalate the cooldown
        exponentially (base -> doubled -> ... -> cap)."""
        s = make_scheduler(
            circuit_threshold=5,
            circuit_cooldown_base=30.0,
            circuit_cooldown=600.0,
        )

        def open_after(failures: int) -> float:
            s._domain_failures.clear()
            s._circuit_trip_count.clear()
            s._circuit_open_until.clear()
            trip_circuit(s, "x", failures=failures)
            return s._circuit_open_until["x"] - time.monotonic()

        assert 29 <= open_after(5) <= 31
        assert 59 <= open_after(10) <= 61
        assert 119 <= open_after(15) <= 121
        assert 599 <= open_after(40) <= 601
        s.record_success("x")
        assert s._circuit_trip_count["x"] == 0

    def test_scheduler_set_domain_min_delay(self):
        """A robots Crawl-delay is enforced as a floor
        on top of the global/adaptive delay."""
        s = make_scheduler(global_limit=2)
        assert s.current_delay("a.com") == 0.0
        s.set_domain_min_delay("a.com", 2.0)
        assert s.current_delay("a.com") == 2.0
        # Star group uses 0; a longer Crawl-delay wins (max semantics).
        s.set_domain_min_delay("a.com", 5.0)
        assert s.current_delay("a.com") == 5.0

    def test_parse_robots_crawl_delay_specific_group_wins(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 1\nUser-agent: crawlutron\nCrawl-delay: 4\n"
        assert _parse_robots_crawl_delay(text) == 4.0

    def test_parse_robots_crawl_delay_star_only(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 3\n"
        assert _parse_robots_crawl_delay(text) == 3.0

    def test_parse_robots_crawl_delay_absent(self):
        from app.crawl.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        assert _parse_robots_crawl_delay("User-agent: *\nDisallow: /\n") is None

    def test_set_domain_min_delay_is_capped_at_the_politeness_ceiling(self):
        """A huge (finite) robots Crawl-delay must not install an
        unbounded politeness floor — the scheduler clamps it at the write."""
        from app.scheduler import _MAX_COOLDOWN_WAIT

        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", 1e12)
        assert scheduler.current_delay("example.com") == _MAX_COOLDOWN_WAIT

    def test_set_domain_min_delay_ignores_non_finite(self):
        """Inf/nan must never become a stored politeness floor."""
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", float("inf"))
        assert "example.com" not in scheduler._domain_min_delay

    async def test_run_streams_results_via_on_result(self):
        """With ``on_result`` set, each completed result is
        delivered immediately (a fast job is delivered before a slow one) and
        ``run`` returns None (nothing accumulated in memory).

        Uses an ``asyncio.Event`` (not ``asyncio.sleep``) to force ordering,
        because the test environment patches ``sleep`` to be instant.
        """
        on_result_order: list[int] = []
        release_slow = asyncio.Event()
        sched = make_scheduler(global_limit=10, per_domain_limit=10)

        async def worker(job: CrawlJob) -> EngineCrawlResult:
            # Make index 0 wait until job 1 has been delivered, so streaming
            # order is observable without a real sleep.
            if job.index == 0:
                await release_slow.wait()
            return make_result(job.url.raw, job.domain)

        async def _on_result(idx: int, _r) -> None:
            on_result_order.append(idx)
            if idx == 1:
                release_slow.set()

        jobs = [make_job(i, f"http://h{i}.example.com/") for i in range(6)]
        ret = await sched.run(jobs, worker, on_result=_on_result, failed_result=engine_envelope)
        # Streaming mode returns None; the caller owns persistence (spill).
        assert ret is None
        # Every job was delivered exactly once.
        assert sorted(on_result_order) == list(range(6))
        # A fast job is delivered before the (gated) slow job -> results stream
        # as they complete rather than being held until the whole batch finishes.
        assert on_result_order.index(1) < on_result_order.index(0)

    async def test_run_returns_results_keyed_by_index_without_on_result(self):
        """Backward-compatible path (recursive frontier) still returns results."""
        sched = make_scheduler(global_limit=10, per_domain_limit=10)

        async def worker(job: CrawlJob) -> EngineCrawlResult:
            return make_result(job.url.raw, job.domain)

        jobs = [make_job(i, f"http://h{i}.example.com/") for i in range(4)]
        ret = await sched.run(jobs, worker, failed_result=engine_envelope)
        assert ret is not None
        # Results are keyed by job index so callers align them with their own job.
        assert sorted(ret) == [0, 1, 2, 3]
        # Canonical, not the seed text: the job's URL is an admitted value, so the
        # trailing slash the seed carried is gone by the time it is a job member.
        assert [ret[i].normalized_url for i in sorted(ret)] == [
            canonical(f"http://h{i}.example.com") for i in range(4)
        ]


class TestCooldownPurgeCadence:
    def test_purge_is_amortized_not_per_rate_limited_result(self, monkeypatch):
        calls = {"n": 0}
        real_purge = DomainScheduler.purge_expired

        def counting_purge(self):
            calls["n"] += 1
            real_purge(self)

        monkeypatch.setattr(DomainScheduler, "purge_expired", counting_purge)
        scheduler = make_scheduler()
        for i in range(250):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        # 250 sets on a 100-set interval: the cadence is amortized, not 1:1.
        # The interval is production's, so the bound is derived rather than
        # repeated: 250 sets over 100-set intervals is three purges.
        assert calls["n"] == 250 // _COOLDOWN_PURGE_INTERVAL

    def test_expired_cooldowns_are_still_reclaimed(self, monkeypatch):
        """Amortizing must not let expired entries accumulate indefinitely:
        once the interval elapses only expired entries drop, and the fresh
        cooldown for the setting domain survives."""
        fake_now = [1000.0]
        monkeypatch.setattr("app.scheduler.time.time", lambda: fake_now[0])
        scheduler = make_scheduler()
        scheduler.set_domain_cooldown("old.test", 10.0)
        fake_now[0] = 1100.0  # the first cooldown has elapsed
        # Burn past the interval so the amortized purge fires.
        for i in range(_COOLDOWN_PURGE_INTERVAL + 1):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        assert "old.test" not in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns, "fresh cooldowns were dropped"

    def test_a_fresh_cooldown_is_never_purged_by_the_amortized_run(self, monkeypatch):
        """The domain that just earned a cooldown is never removed by the purge
        that follows its own set."""
        monkeypatch.setattr("app.scheduler.time.time", lambda: 1000.0)
        scheduler = make_scheduler()
        scheduler.set_domain_cooldown("rate-limited.test", 30.0)
        assert "rate-limited.test" in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns["rate-limited.test"] > 1000.0


def test_run_still_purges_up_front():
    """The batch entry point keeps its up-front purge so a long run cannot grow
    the dicts without bound even when no 429 fires."""
    scheduler = make_scheduler()
    scheduler._domain_cooldowns["stale.test"] = 0.0  # already expired
    assert "stale.test" in scheduler._domain_cooldowns
    # The up-front purge fires before any job runs.
    scheduler.purge_expired()
    assert "stale.test" not in scheduler._domain_cooldowns


class TestSchedulerResilience:
    async def test_scheduler_continues_other_jobs_on_single_failure(self):
        """One failing job must not cancel the others; results for the
        successful jobs are still returned."""
        scheduler = make_scheduler(global_limit=5, per_domain_limit=5)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]

        async def failing_worker(job: CrawlJob) -> EngineCrawlResult:
            if job.index == 1:
                raise RuntimeError("transient failure")
            return make_result(job.url.raw, job.domain)

        results = await scheduler.run(jobs, failing_worker, failed_result=engine_envelope)
        assert results is not None
        # The failing job is not dropped: the chokepoint builds it a classified
        # terminal envelope (reading the URL pair off whichever shape the job
        # carries -- CrawlJob has only ``url``), so the caller never observes a
        # missing slot and improvises a result off the raw error. The other
        # jobs are unaffected.
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

    The hosts must be registrable-distinct: ``domain`` is the registrable
    domain, so two ``example.com`` hosts are one domain sharing one delay.
    """
    scheduler = make_scheduler(_PerDomainDelayScheduler, adaptive=False)
    fast_started = asyncio.Event()
    starved: list[str] = []
    yield_to_loop = asyncio.sleep

    async def _sleep(delay, *args, **kwargs):
        del args, kwargs
        if delay >= 0.2:
            # Slow.example.com's 0.4s per-domain delay (jittered). It may not
            # finish until the fast domain has been served. The starvation is
            # recorded rather than raised because ``run`` gathers with
            # return_exceptions=True, which would swallow an exception raised
            # from inside a worker's own await chain.
            try:
                await asyncio.wait_for(fast_started.wait(), 2.0)
            except TimeoutError:
                starved.append("fast.example.org")
            return
        await yield_to_loop(0)

    monkeypatch.setattr(asyncio, "sleep", _sleep)

    async def worker(job):
        if job.domain == "example.org":
            fast_started.set()
        await yield_to_loop(0)
        return make_result(job.url.raw, job.domain)

    jobs = [make_job(0, "http://slow.example.com/"), make_job(1, "http://fast.example.org/")]
    await scheduler.run(jobs, worker, failed_result=engine_envelope)
    assert not starved, (
        "fast.example.org could not run until slow.example.com's per-domain delay "
        "finished: the delay is being awaited while holding a GLOBAL concurrency "
        "slot, so it burns that slot on pure waiting and starves every other domain"
    )
    assert fast_started.is_set(), "the fast domain was never served"


async def test_zero_delay_requests_no_sleep_at_all(monkeypatch):
    """The inverse: a domain configured with no delay must request no wait.

    The suite's autouse patch now makes every sleep zero-second *and* yielding,
    so "the delay was skipped" is no longer observable as elapsed time -- but
    it is still observable as a request. A scheduler that turned a zero
    configured delay into a positive sleep request would be inventing politeness
    the operator never asked for, and paying it on every single job.
    """
    sleeps = recorder(monkeypatch)
    scheduler = make_scheduler(adaptive=False)
    jobs = [make_job(0, "https://example.com/a"), make_job(1, "https://example.com/b")]
    await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
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

        The old shape delivered a second result for the same job -- the
        ``failed_result`` envelope -- which a spill-based sink would have
        written to disk as though the crawl had failed.
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

    The success/failure branch tested membership in ``("failed",
    "network_error")``, which reads as though a network fault got its own
    accounting. It does not: ``network_error`` is a taxonomy member describing
    WHY a page failed, and ``status_for_classification`` maps it ONTO
    "failed" -- it is never a status itself, so the element could not match
    anything. Meanwhile four of the six ResultStatus members matched neither
    branch, and nothing noticed.

    Total by construction now: the verdicts live in one table keyed by status,
    and the exhaustiveness test fails if a status is added without one.
    """

    @staticmethod
    def _terminal(job: CrawlJob, classification: Classification):
        """A real terminal result, with its status derived by the real chokepoint."""
        from app.constants import status_for_classification
        from app.crawl.result_helpers import _terminal_engine_result

        # Status is the outer function's own keyword, not a field of the shared
        # value: it is what the per-status constructors pin, and a
        # CONTEXT_VARYING_STATUS classification needs one the table cannot
        # derive. See _terminal_engine_result.
        return _terminal_engine_result(
            TerminalResult(
                original_url=job.url.raw,
                normalized_url=job.url.raw,
                strategy=None,
                classification=classification,
                # ``error`` is a taxonomy code, not an optional: production's own
                # envelope passes the classification through it, and a result
                # carrying no error code is a shape no caller produces.
                error=classification,
            ),
            status=status_for_classification(classification),
        )

    async def test_a_network_fault_records_a_domain_failure(self):
        """The reported case, through the real failure envelope, not a copy."""
        import socket

        scheduler = make_scheduler(circuit_cooldown=0.0)
        job = make_job(0, "https://example.com/p")

        async def failing_worker(job):
            return engine_envelope(job, socket.gaierror("name resolution failed"))

        results = await scheduler.run([job], failing_worker, failed_result=engine_envelope)
        assert results is not None
        result = results[job.index]
        # Different axes: the status is the verdict, the classification the reason.
        # Comparing one against the other is why the old tuple could not work.
        assert result.status == "failed"
        assert result.classification == "dns_error"
        assert scheduler._domain_failures.get("example.com", 0) == 1

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
        scheduler = make_scheduler(circuit_cooldown=0.0)
        job = make_job(0, "https://example.com/p")

        async def worker(job):
            return self._terminal(job, classification)

        await scheduler.run([job], worker, failed_result=engine_envelope)
        assert scheduler._domain_failures.get("example.com", 0) == expected_failures, why

    def test_status_accounting_is_total(self):
        """Every ResultStatus member has a verdict, so none can arrive unhandled.

        The branch this replaced tested membership in a two-element tuple, so
        blocked, disallowed, retry_exhausted and skipped matched nothing, and a
        status added to the taxonomy later would have silently joined them
        rather than forcing a decision.
        """
        import typing

        from app.constants import ResultStatus
        from app.scheduler import _STATUS_ACCOUNTING

        members = set(typing.get_args(ResultStatus))
        assert members, "ResultStatus must be a Literal to be checkable"
        assert not members - set(_STATUS_ACCOUNTING), (
            f"a ResultStatus member has no domain-health verdict: {sorted(members - set(_STATUS_ACCOUNTING))}"
        )
        assert not set(_STATUS_ACCOUNTING) - members, (
            "the accounting table names a status the taxonomy does not define: "
            f"{sorted(set(_STATUS_ACCOUNTING) - members)}"
        )
        assert set(_STATUS_ACCOUNTING.values()) <= {"success", "failure", "none"}
