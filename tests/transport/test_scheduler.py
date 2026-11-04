"""The run queue admits, throttles and breaks circuits per domain."""

import pytest

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_task import CrawlTask
from crawlutron.transport.scheduler import DomainScheduler, jittered_delay
from tests._policy_factories import canonical
from tests._result_builders import engine_result, make_job
from tests._scheduler_doubles import engine_envelope, make_scheduler, ok_worker, trip_circuit
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
    @staticmethod
    def _cooled_rig(monkeypatch, *, window: float):
        """The injected-clock cooldown rig: recorder, mutable clock, a scheduler
        with a stamped example.com debt, and one job on that domain."""
        sleeps = recorder(monkeypatch)
        now = [1000.0]
        scheduler = make_scheduler(delay_seconds=0.05, clock=lambda: now[0])
        scheduler.set_domain_cooldown("example.com", window)
        return sleeps, now, scheduler, make_job(0, "http://example.com/")

    def test_jitter_in_range(self):
        scheduler = make_scheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        base = scheduler._delay_seconds
        delays = [jittered_delay(base) for _ in range(50)]
        assert all(0.7 <= d <= 1.3 for d in delays), delays
        # A zero base must not sleep at all, whatever the jitter draws. This branch is the reason the band is
        # not applied unconditionally.
        assert jittered_delay(0.0) == 0.0

    def test_set_domain_cooldown_keeps_the_record_live_for_the_window(self):
        """A fresh cooldown parks the domain for (just under) the declared
        window on the injected clock -- behavior, not a wall-time stamp pin."""
        now = [1000.0]
        scheduler = make_scheduler(
            global_limit=5, per_domain_limit=3, delay_seconds=0.1, clock=lambda: now[0]
        )
        scheduler.set_domain_cooldown("example.com", 5.0)
        scheduler.purge_expired()
        assert "example.com" in scheduler.domain_cooldowns
        # Still standing at window-1; gone at window+1 (the purge is the
        # public door for "is the debt still owed").
        now[0] += 4.0
        scheduler.purge_expired()
        assert "example.com" in scheduler.domain_cooldowns
        now[0] += 2.0
        scheduler.purge_expired()
        assert "example.com" not in scheduler.domain_cooldowns

    async def test_cooldown_naps_only_the_remaining_window(self, monkeypatch):
        """A cooldown's park is the REMAINING duration on the one clock, and a
        served cooldown requests no nap at all (#580): stamp and read share the
        injected clock, so no wall-clock step can bend the wait either way."""
        sleeps, now, scheduler, job = self._cooled_rig(monkeypatch, window=0.3)
        now[0] += 0.15  # half the window elapsed before the batch fires

        async def worker(ran_job: CrawlJob) -> EngineCrawlResult:
            return engine_result(ran_job.url.raw, "rendered")

        results = await scheduler.run([job], worker, failed_result=engine_envelope)
        assert results is not None and results[0].status == "success"
        # Each park reads the remaining window on the ONE clock (F2 double-check).
        naps = [d for d in sleeps.delays if d >= 0.1]
        assert naps == [pytest.approx(0.15, abs=1e-9)] * len(naps) and naps, (
            "the park must be the remaining window on the ONE clock, not a "
            f"re-derivation from another family: {sleeps.delays}"
        )

        # Served: the same domain re-fires with no cooldown debt left. The
        # recorder accumulates across runs, so compare against the snapshot.
        before = len(sleeps.delays)
        now[0] += 1.0
        results = await scheduler.run([job], worker, failed_result=engine_envelope)
        served = results is not None and results[0].status == "success"
        assert served and not any(d >= 0.1 for d in sleeps.delays[before:]), (
            f"an elapsed cooldown requested a nap: {sleeps.delays[before:]}"
        )

    async def test_cooldown_delay_applied_to_domain(self, monkeypatch):
        """A domain in cooldown must have the cooldown awaited before the worker.

        Asserted on the *request*, not the wall time -- and on the INJECTED
        clock, so the window cannot decay while the worker spins up (the real
        monotonic clock made the 90% floor a timing race under xdist)."""
        sleeps, now, scheduler, job = self._cooled_rig(monkeypatch, window=0.3)

        async def worker(ran_job: CrawlJob) -> EngineCrawlResult:
            assert sleeps.requests_over(0.27), (
                "the worker ran before the domain cooldown was awaited; the "
                f"recorded sleep requests were {sleeps.delays}"
            )
            return engine_result(ran_job.url.raw, "rendered")

        # The delivery channel decides the return: with no sink, ``run`` hands back the index-keyed mapping
        # (the docstring's "otherwise" branch).
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
        # Run reports a job.index -> result mapping so callers align each result with its own job regardless
        # of completion order.
        assert sorted(results) == [0, 1, 2]
        assert [results[i].normalized_url for i in sorted(results)] == [
            canonical(f"http://example.com/{i}") for i in range(3)
        ]

    async def test_delay_applied_between_same_domain_jobs(self, monkeypatch):
        """#577: same-domain fires are SPACED, not staggered once.

        The first fire waits nothing and the second waits one jittered delay --
        the old herd requested two copies of the delay and fired together.
        """
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(delay_seconds=0.1, adaptive=False)
        jobs = [make_job(0, "http://example.com/a"), make_job(1, "http://example.com/b")]
        results = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert results is not None
        spacing = sleeps.in_range(0.08, 0.12)
        assert len(spacing) == 1, f"one spaced fire past the free first fire, saw {sleeps.delays}"


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

    The scheduler once read ``diagnostics["runtime"]``, which only the crawl
    model carries -- so render pipeline latencies were silently dropped.
    """

    @staticmethod
    def _rendered_result(elapsed_ms: int):
        from crawlutron.models.crawl_result import CrawlResult
        from crawlutron.models.result_factory import terminal_attempt

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
        from crawlutron.models.crawl_strategy_attempt import CrawlStrategyAttempt
        from crawlutron.models.result_factory import build_engine_result

        return build_engine_result(
            original_url="https://example.com/x",
            normalized_url="https://example.com/x",
            final_url="https://example.com/x",
            attempt=CrawlStrategyAttempt(
                strategy="HTTP_DECLARED_CRAWLER",
                status="success",
                classification="rendered",
                http_status=200,
                content_type="text/html",
                elapsed_ms=elapsed_ms,
            ),
            http_status=200,
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
        # Same baseline the sibling test asserts for an unlearned domain: the configured fixed delay,
        # unchanged because nothing was observed.
        assert s.current_delay("example.com") == 1.0


class TestCircuitBreakerSpillsOnResult:
    """With ``on_result`` set, an open circuit must spill the failed envelope through it.

    Writing ``results[index]`` in that mode is ``None`` -> TypeError, swallowed
    by ``return_exceptions=True`` -> the URL silently vanishes.
    """

    async def test_open_circuit_spills_via_on_result(self, monkeypatch):
        recorder(monkeypatch)  # the park a 429 takes; zero wall clock
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
        )
        # The delivery channel decides the return: with no sink, ``run`` hands back the index-keyed mapping
        # (the docstring's "otherwise" branch).
        returned = await scheduler.run([job], worker, on_result=on_result, failed_result=engine_envelope)

        assert returned is None, "on_result mode must return None"
        assert len(spilled) == 1, f"expected 1 spilled result, got {len(spilled)}"
        index, result = spilled[0]
        assert index == 0
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
