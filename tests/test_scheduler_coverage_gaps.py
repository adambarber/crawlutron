"""TestSchedulerCoverageGaps coverage tests (split from tests/test_coverage_gaps.py)."""

from coverage_helpers import *  # noqa: F401,F403

class TestSchedulerCoverageGaps:
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
        from app.scheduler import DomainScheduler
        import time
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
        from app.scheduler import DomainScheduler
        import time

        s = DomainScheduler(
            global_limit=1, per_domain_limit=1, delay_seconds=0.0,
            circuit_threshold=5, circuit_cooldown_base=30.0, circuit_cooldown=600.0,
        )

        def open_after(failures: int) -> float:
            s._domain_failures.clear(); s._circuit_trip_count.clear(); s._circuit_open_until.clear()
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
        from app.crawl.engine import _parse_robots_crawl_delay
        text = (
            "User-agent: *\n"
            "Crawl-delay: 1\n"
            "User-agent: dev-stack-browser-crawler\n"
            "Crawl-delay: 4\n"
        )
        assert _parse_robots_crawl_delay(text) == 4.0

    def test_parse_robots_crawl_delay_star_only(self):
        from app.crawl.engine import _parse_robots_crawl_delay
        text = "User-agent: *\nCrawl-delay: 3\n"
        assert _parse_robots_crawl_delay(text) == 3.0

    def test_parse_robots_crawl_delay_absent(self):
        from app.crawl.engine import _parse_robots_crawl_delay
        assert _parse_robots_crawl_delay("User-agent: *\nDisallow: /\n") is None
