"""The run queue admits, throttles and breaks circuits per domain."""

import time

import pytest

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.orchestration.scheduler import DomainScheduler, jittered_delay
from tests._policy_factories import canonical
from tests._result_builders import make_job, make_result
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
    def test_jitter_in_range(self):
        scheduler = make_scheduler(global_limit=5, per_domain_limit=3, delay_seconds=1.0)
        base = scheduler._delay_seconds
        delays = [jittered_delay(base) for _ in range(50)]
        assert all(0.7 <= d <= 1.3 for d in delays), delays
        # A zero base must not sleep at all, whatever the jitter draws. This branch is the reason the band is
        # not applied unconditionally.
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
        from app.models.crawl_result import CrawlResult
        from app.orchestration.render_result import terminal_attempt

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
        from app.models.crawl_result import CrawlStrategyAttempt
        from app.orchestration.engine_result import build_engine_result

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
