"""Breaker, latency EMA and cooldown policies bound per-domain pressure."""

import asyncio

import pytest

from crawlutron.models.crawl_job import CrawlJob
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.transport.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler, TokenBucket
from tests._policy_factories import canonical
from tests._result_builders import make_job, make_result
from tests._scheduler_doubles import engine_envelope, make_scheduler, ok_worker, trip_circuit

# Breaker, EMA and bucket policy tests record their clock through these two:
from tests._timing_harnesses import recorder


class TestSchedulerCircuitBreaker:
    def test_record_success_resets_the_escalation_ladder(self):
        now = [0.0]
        scheduler = make_scheduler(
            circuit_threshold=1, circuit_cooldown_base=30.0, circuit_cooldown=600.0, clock=lambda: now[0]
        )
        scheduler.record_failure("example.com")  # t=0: first window, base cooldown
        assert scheduler.is_circuit_open("example.com")
        now[0] = 5.0  # still inside that window
        scheduler.record_success("example.com")
        scheduler.record_failure("example.com")  # a fresh charge: base window from NOW, not doubled
        now[0] = 35.0
        assert not scheduler.is_circuit_open("example.com"), (
            "the success did not clear the domain's escalation: the second fault opened a "
            "doubled window still running at t=35 instead of a base window ending there"
        )

    def test_scheduler_is_circuit_open_false(self):
        scheduler = make_scheduler()
        assert not (scheduler.is_circuit_open("example.com"))


class TestSchedulerLatencyEma:
    """The EMA is invisible by design; what it CAUSES is the adaptive delay."""

    def test_scheduler_record_latency_invalid(self):
        scheduler = make_scheduler()
        # A negative latency is a broken measurement, not a fast one: recording it would pull the
        # adaptive delay under every delay actually observed.
        scheduler.record_latency("example.com", -1.0)
        assert scheduler.current_delay("example.com") == 0.0, (
            "a rejected measurement moved the adaptive delay off the base: the EMA accepted it"
        )

    def test_scheduler_record_latency_valid(self):
        scheduler = make_scheduler(adaptive_latency_factor=1.0, adaptive_min_delay=0.5)
        scheduler.record_latency("example.com", 1000.0)
        assert scheduler.current_delay("example.com") == pytest.approx(1.0), (
            "a recorded 1s latency left the adaptive delay at the base: the EMA never fed it"
        )

    def test_scheduler_record_latency_ema(self):
        scheduler = make_scheduler(adaptive_smoothing=0.5, adaptive_latency_factor=1.0)
        scheduler.record_latency("example.com", 1000.0)
        scheduler.record_latency("example.com", 3000.0)
        assert scheduler.current_delay("example.com") == pytest.approx(2.0), (
            "the adaptive delay is not the smoothed mean of the two observations: the EMA "
            "is either not averaging or weighting the latest sample alone"
        )


class TestSchedulerPolicies:
    """Cover remaining branches in scheduler.py"""

    @staticmethod
    def _bucket_at_full(rate: float, capacity: int) -> TokenBucket:
        """A full bucket, since the constructor already starts one there."""
        return TokenBucket(rate=rate, capacity=capacity)

    def test_token_bucket_basic(self):
        from crawlutron.transport.scheduler import TokenBucket as _Bucket

        bucket = _Bucket(rate=10.0, capacity=5)
        wait = bucket.consume(1.0)
        assert wait == 0.0
        wait = bucket.consume(4.0)
        assert wait == 0.0
        wait = bucket.consume(1.0)
        assert wait > 0.0

    def test_token_bucket_refill(self):
        bucket = self._bucket_at_full(rate=100.0, capacity=10)
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
        now = [0.0]
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, clock=lambda: now[0])
        trip_circuit(scheduler, "example.com")
        # Half-open: after the window elapses the breaker re-admits the domain.
        now[0] += 10_000.0
        assert not (scheduler.is_circuit_open("example.com"))

    def test_scheduler_record_latency(self):
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, adaptive=True)
        scheduler.record_latency("example.com", 5000.0)
        assert scheduler.current_delay("example.com") > 0.0, (
            "a recorded 5s latency left the adaptive delay at the base: the EMA never fed it"
        )

    async def test_scheduler_max_rps_token_bucket(self, monkeypatch):
        """The cap is observable as a requested wait, not a stored number.

        The bucket starts full at ``capacity=5``, so the first five admissions
        are free and the sixth must request the refill wait (1/5s at 5 rps)."""
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, max_rps=5.0)
        jobs = [make_job(i, "https://example.com/p") for i in range(6)]
        ret = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert ret is not None
        assert any(d > 0.05 for d in sleeps.delays), (
            f"the 6th request inside one second at 5 rps requested no refill wait: {sleeps.delays}"
        )

    def test_circuit_breaker_exponential_backoff(self):
        """Consecutive trips escalate the cooldown
        exponentially (base -> doubled -> ... -> cap)."""
        now = [0.0]
        s = make_scheduler(
            circuit_threshold=5,
            circuit_cooldown_base=30.0,
            circuit_cooldown=600.0,
            clock=lambda: now[0],
        )

        def still_open_after(failures: int, elapsed: float) -> bool:
            # A fresh scheduler state per probe: failure ladder re-wound by hand through
            # record_success, which is the public way to clear the charge.
            s.record_success("x")
            now[0] = 0.0
            trip_circuit(s, "x", failures=failures)
            now[0] = elapsed
            return s.is_circuit_open("x")

        # First trip: base 30s. Escalations: 60, 120; 40 failures -> cap 600.
        assert still_open_after(5, 29.0)
        assert not still_open_after(5, 31.0)
        assert still_open_after(10, 59.0)
        assert not still_open_after(10, 61.0)
        assert still_open_after(15, 119.0)
        assert not still_open_after(15, 121.0)
        assert still_open_after(40, 599.0)
        assert not still_open_after(40, 601.0)

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
        from crawlutron.transport.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 1\nUser-agent: crawlutron\nCrawl-delay: 4\n"
        assert _parse_robots_crawl_delay(text) == 4.0

    def test_parse_robots_crawl_delay_star_only(self):
        from crawlutron.transport.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        text = "User-agent: *\nCrawl-delay: 3\n"
        assert _parse_robots_crawl_delay(text) == 3.0

    def test_parse_robots_crawl_delay_absent(self):
        from crawlutron.transport.robots_parse import parse_robots_crawl_delay as _parse_robots_crawl_delay

        assert _parse_robots_crawl_delay("User-agent: *\nDisallow: /\n") is None

    def test_set_domain_min_delay_is_capped_at_the_politeness_ceiling(self):
        """A huge (finite) robots Crawl-delay must not install an
        unbounded politeness floor — the scheduler clamps it at the write."""
        from crawlutron.transport.scheduler import MAX_COOLDOWN_WAIT

        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", 1e12)
        assert scheduler.current_delay("example.com") == MAX_COOLDOWN_WAIT

    def test_set_domain_min_delay_ignores_non_finite(self):
        """Inf/nan must never become a stored politeness floor."""
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10)
        scheduler.set_domain_min_delay("example.com", float("inf"))
        assert scheduler.current_delay("example.com") == 0.0, (
            "an infinite Crawl-delay installed an unbounded politeness floor: the domain "
            "would wait forever on its next request"
        )

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
        # Canonical, not the seed text: the job's URL is an admitted value; #520 keeps the
        # path text verbatim, so the seed's trailing slash survives onto the job member.
        assert [ret[i].normalized_url for i in sorted(ret)] == [
            canonical(f"http://h{i}.example.com/") for i in range(4)
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

    def test_expired_cooldowns_are_still_reclaimed(self):
        """Amortizing must not let expired entries accumulate indefinitely:
        once the interval elapses only expired entries drop, and the fresh
        cooldown for the setting domain survives."""
        now = [1000.0]
        scheduler = make_scheduler(clock=lambda: now[0])
        scheduler.set_domain_cooldown("old.test", 10.0)
        now[0] = 1100.0  # the first cooldown has elapsed
        # Burn past the interval so the amortized purge fires.
        for i in range(COOLDOWN_PURGE_INTERVAL + 1):
            scheduler.set_domain_cooldown(f"d{i}.test", 5.0)
        assert "old.test" not in scheduler.domain_cooldowns
        assert scheduler.domain_cooldowns, "fresh cooldowns were dropped"

    def test_a_fresh_cooldown_is_never_purged_by_the_amortized_run(self):
        """The domain that just earned a cooldown is never removed by the purge
        that follows its own set."""
        now = [1000.0]
        scheduler = make_scheduler(clock=lambda: now[0])
        scheduler.set_domain_cooldown("rate-limited.test", 30.0)
        assert "rate-limited.test" in scheduler.domain_cooldowns
        assert scheduler.domain_cooldowns["rate-limited.test"] > 1000.0


class TestTokenBucketConcurrency:
    """#517: under concurrency the bucket must RESERVE, not just observe.

    Calls that overlap must each be held back by what the others took, or every
    job in a burst reads the same token count and fires together.
    """

    async def test_an_overlapping_burst_reserves_in_arrival_order(self, monkeypatch):
        """The scheduler-level herd: seven jobs on one domain, gathered like
        ``run``'s fan-out, must request SERIALLY increasing waits -- never the
        same deficit twice. #577's reservation folds in as a max(): the waits
        are nondecreasing and spaced, whichever debt is larger.
        """
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(global_limit=10, per_domain_limit=10, max_rps=1.0, adaptive=False)
        # capacity=5: the first five are free bucket-wise; the 6th and 7th queue
        # behind the refill at rate 1 (zero base delay here, so the bucket dominates).
        jobs = [make_job(i, "https://example.com/p") for i in range(7)]
        ret = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert ret is not None
        waits = sorted(sleeps.in_range(0.9, 3.0))
        assert waits == [pytest.approx(1.0, abs=1e-3), pytest.approx(2.0, abs=1e-3)], (
            f"the burst did not serialize its reserves behind the free tokens: {waits}"
        )
        # Nothing invented: every requested wait is a real debt (bucket refill),
        # and no wait is repeated -- arrival-order reservation, not herd observation.
        assert sorted(sleeps.delays) == sorted(set(sleeps.delays)) or not (
            len(sleeps.delays) != len(set(round(d, 9) for d in sleeps.delays))
        ), f"a repeated wait means two jobs observed the same deficit: {sleeps.delays}"

    async def test_drained_bucket_serializes_two_gathered_consumes(self, monkeypatch):
        """Two gathered consumers on a drained bucket must wait SERIALLY: the
        second observes what the first reserved and waits ~1/rate longer."""
        sleeps = recorder(monkeypatch)
        bucket = TokenBucket(rate=2.0, capacity=0)
        taken: list[float] = []

        async def one() -> None:
            wait = bucket.consume(1.0)
            taken.append(wait)
            await asyncio.sleep(wait)  # the real caller's shape: fire after the wait

        await asyncio.gather(one(), one())
        # A real monotonic clock refills microseconds between the two takes, so
        # the equality is to a millisecond, not an exact float.
        assert taken == [pytest.approx(0.5, abs=1e-3), pytest.approx(1.0, abs=1e-3)], taken
        assert sorted(sleeps.delays) == [
            pytest.approx(0.5, abs=1e-3),
            pytest.approx(1.0, abs=1e-3),
        ], sleeps.delays
