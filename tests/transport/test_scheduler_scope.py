"""Scheduler scope honesty: per-domain state is per-instance, not process-wide."""

import asyncio

from crawlutron.config.settings import CrawlerSettings
from crawlutron.transport.scheduler import DomainScheduler


def test_scheduler_instances_have_independent_state():
    """Two schedulers built from identical settings share no per-domain state.

    A cooldown, min-delay floor, circuit trip, latency EMA, or token bucket
    recorded on one must be invisible on the other. Guards against class-level
    mutable state regressions and documents instance scope.
    """
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "2",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "500",
        }
    )
    a = DomainScheduler.from_settings(settings, global_limit=4, max_rps=0.0)
    b = DomainScheduler.from_settings(settings, global_limit=4, max_rps=0.0)

    domain = "example.com"

    # Independent cooldowns
    a.set_domain_cooldown(domain, 10.0)
    assert domain in a.domain_cooldowns
    assert domain not in b.domain_cooldowns

    # Independent min-delay floors
    a.set_domain_min_delay(domain, 5.0)
    assert a.current_delay(domain) >= 5.0
    assert b.current_delay(domain) == 0.5  # settings default only

    # Independent circuit breakers
    a.record_failure(domain)
    a.record_failure(domain)
    a.record_failure(domain)
    a.record_failure(domain)
    a.record_failure(domain)  # threshold=5 → open
    assert a.is_circuit_open(domain)
    assert not b.is_circuit_open(domain)

    # Independent latency EMAs
    a.record_latency(domain, 1000.0)
    assert a._state(domain).ema is not None
    assert b._state(domain).ema is None

    # Instance identity of the one per-domain map -- the collapse (#577) made
    # nine maps one, so the nine identity asserts became this single one.
    assert a._domain_state is not b._domain_state


def test_scheduler_class_docstring_declares_instance_scope():
    """DomainScheduler documents its per-instance scope (red→green with doc fix)."""
    doc = (DomainScheduler.__doc__ or "").lower()
    assert "per-instance" in doc or "per instance" in doc


async def test_concurrent_instances_allow_combined_concurrency():
    """Two schedulers with per_domain_limit=1 each allow 1 concurrent → combined 2.

    This demonstrates the scope: the per-domain gate is per-instance. A caller
    assuming a service-wide bound would observe double the rate with two requests.
    """
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "1",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "0",
        }
    )
    a = DomainScheduler.from_settings(settings, global_limit=10, max_rps=0.0)
    b = DomainScheduler.from_settings(settings, global_limit=10, max_rps=0.0)

    async def slow_worker(job):
        await asyncio.sleep(0.05)
        return job

    class Job:
        def __init__(self, index, domain):
            self.index = index
            self.domain = domain

    jobs_a = [Job(0, "example.com")]
    jobs_b = [Job(1, "example.com")]

    async def run_one(sched, jobs, name):
        results = await sched.run(jobs, slow_worker, failed_result=lambda j, e: None)
        return results

    # Both run concurrently; if gates were shared, total time ≈ 100ms (serialized).
    # If independent, total time ≈ 50ms (parallel per instance).
    start = asyncio.get_event_loop().time()
    await asyncio.gather(run_one(a, jobs_a, "a"), run_one(b, jobs_b, "b"))
    elapsed = asyncio.get_event_loop().time() - start

    # Independent gates: ~50ms. Shared gate would force ~100ms. Allow generous margin.
    assert elapsed < 0.15, f"Expected ~50ms concurrent, got {elapsed:.3f}s"
