"""Breaker, latency EMA and cooldown policies bound per-domain pressure."""

import asyncio
import time

from app.models.crawl_job import CrawlJob
from app.models.crawl_result import EngineCrawlResult
from app.orchestration.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler
from tests._policy_factories import canonical
from tests._result_builders import make_job, make_result
from tests._scheduler_doubles import engine_envelope, make_scheduler, trip_circuit


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
        # A negative latency is a broken measurement, not a fast one: recording it would pull the EMA under
        # every delay actually observed and let the domain through faster than anything it has been timed at.
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
        from app.orchestration.scheduler import TokenBucket

        bucket = TokenBucket(rate=10.0, capacity=5)
        wait = bucket.consume(1.0)
        assert wait == 0.0
        wait = bucket.consume(4.0)
        assert wait == 0.0
        wait = bucket.consume(1.0)
        assert wait > 0.0

    def test_token_bucket_refill(self):
        from app.orchestration.scheduler import TokenBucket

        bucket = TokenBucket(rate=100.0, capacity=10)
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
        from app.orchestration.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 1\nUser-agent: crawlutron\nCrawl-delay: 4\n"
        assert _parse_robots_crawl_delay(text) == 4.0

    def test_parse_robots_crawl_delay_star_only(self):
        from app.orchestration.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 3\n"
        assert _parse_robots_crawl_delay(text) == 3.0

    def test_parse_robots_crawl_delay_absent(self):
        from app.orchestration.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        assert _parse_robots_crawl_delay("User-agent: *\nDisallow: /\n") is None

    def test_set_domain_min_delay_is_capped_at_the_politeness_ceiling(self):
        """A huge (finite) robots Crawl-delay must not install an
        unbounded politeness floor — the scheduler clamps it at the write."""
        from app.orchestration.scheduler import MAX_COOLDOWN_WAIT

        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", 1e12)
        assert scheduler.current_delay("example.com") == MAX_COOLDOWN_WAIT

    def test_set_domain_min_delay_ignores_non_finite(self):
        """Inf/nan must never become a stored politeness floor."""
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", float("inf"))
        assert "example.com" not in scheduler._domain_min_delay

    async def test_run_streams_results_via_on_result(self):
        """With ``on_result`` set, each completed result is delivered immediately.

        A fast job is delivered before a slow one and ``run`` returns None;
        ``asyncio.Event`` forces the order since the env patches ``sleep``.
        """
        on_result_order: list[int] = []
        release_slow = asyncio.Event()
        sched = make_scheduler(global_limit=10, per_domain_limit=10)

        async def worker(job: CrawlJob) -> EngineCrawlResult:
            # Make index 0 wait until job 1 has been delivered, so streaming order is observable without a
            # real sleep.
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
        # A fast job is delivered before the (gated) slow job -> results stream as they complete rather than
        # being held until the whole batch finishes.
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
        # Canonical, not the seed text: the job's URL is an admitted value, so the trailing slash the seed
        # carried is gone by the time it is a job member.
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
        # 250 sets on a 100-set interval: the cadence is amortized, not 1:1. The interval is production's, so
        # the bound is derived rather than repeated: 250 sets over 100-set intervals is three purges.
        assert calls["n"] == 250 // COOLDOWN_PURGE_INTERVAL

    def test_expired_cooldowns_are_still_reclaimed(self, monkeypatch):
        """Amortizing must not let expired entries accumulate indefinitely:
        once the interval elapses only expired entries drop, and the fresh
        cooldown for the setting domain survives."""
        fake_now = [1000.0]
        monkeypatch.setattr("app.orchestration.scheduler.time.time", lambda: fake_now[0])
        scheduler = make_scheduler()
        scheduler.set_domain_cooldown("old.test", 10.0)
        fake_now[0] = 1100.0  # the first cooldown has elapsed
        # Burn past the interval so the amortized purge fires.
        for i in range(COOLDOWN_PURGE_INTERVAL + 1):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        assert "old.test" not in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns, "fresh cooldowns were dropped"

    def test_a_fresh_cooldown_is_never_purged_by_the_amortized_run(self, monkeypatch):
        """The domain that just earned a cooldown is never removed by the purge
        that follows its own set."""
        monkeypatch.setattr("app.orchestration.scheduler.time.time", lambda: 1000.0)
        scheduler = make_scheduler()
        scheduler.set_domain_cooldown("rate-limited.test", 30.0)
        assert "rate-limited.test" in scheduler._domain_cooldowns
        assert scheduler._domain_cooldowns["rate-limited.test"] > 1000.0
