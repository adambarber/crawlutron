"""Scheduler policy units: token buckets, circuit breakers, robots crawl delays."""

from coverage_helpers import *


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
        from app.scheduler import DomainScheduler
        from tests._testkit import make_job, make_result

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
        ret = await sched.run(jobs, worker, on_result=_on_result)
        # Streaming mode returns None; the caller owns persistence (spill).
        assert ret is None
        # Every job was delivered exactly once.
        assert sorted(on_result_order) == list(range(6))
        # A fast job is delivered before the (gated) slow job -> results stream
        # as they complete rather than being held until the whole batch finishes.
        assert on_result_order.index(1) < on_result_order.index(0)

    async def test_run_returns_results_keyed_by_index_without_on_result(self):
        """Backward-compatible path (recursive frontier) still returns results."""
        from app.scheduler import DomainScheduler
        from tests._testkit import make_job, make_result

        sched = DomainScheduler(global_limit=10, per_domain_limit=10, delay_seconds=0.0)

        async def worker(job: CrawlJob):
            return make_result(job.url, job.domain)

        jobs = [make_job(i, f"http://h{i}.example.com/") for i in range(4)]
        ret = await sched.run(jobs, worker)
        assert ret is not None
        # Results are keyed by job index so callers align them with their own job.
        assert sorted(ret) == [0, 1, 2, 3]
        assert [ret[i].normalized_url for i in sorted(ret)] == [
            f"http://h{i}.example.com/" for i in range(4)
        ]
