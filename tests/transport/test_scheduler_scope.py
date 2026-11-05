"""Scheduler scope honesty: per-domain state is per-instance, not process-wide."""

import asyncio

import pytest

from crawlutron.config.settings import CrawlerSettings
from crawlutron.transport.scheduler import DomainScheduler
from tests._timing_harnesses import HANG_GUARD_SECONDS


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

    class Job:
        def __init__(self, index, domain):
            self.index = index
            self.domain = domain

    jobs_a = [Job(0, "example.com")]
    jobs_b = [Job(1, "example.com")]

    # A rendezvous, not a stopwatch: the two workers must both be inside their
    # jobs at once, which only independent gates permit.
    both_running = asyncio.Barrier(2)

    async def slow_worker(job):
        async with both_running:
            return job

    results = await asyncio.wait_for(
        asyncio.gather(
            a.run(jobs_a, slow_worker, failed_result=lambda j, e: None),
            b.run(jobs_b, slow_worker, failed_result=lambda j, e: None),
        ),
        timeout=HANG_GUARD_SECONDS,
    )
    # run() returns per-job result dicts keyed by job index; both completed.
    for result in results:
        assert result is not None
        assert list(result) == [0] or list(result) == [1]


def test_the_gather_of_two_instances_cannot_outlast_a_shared_gate():
    """Red check for the conversion: a SHARED gate serializes the two workers, so
    the rendezvous barrier above cannot be reached by both -- the point of the
    scope test, stated as a hang-guard instead of a stopwatch claim."""

    class SharedJob:
        def __init__(self, index):
            self.index = index
            self.domain = "example.com"

    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "1",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "0",
        }
    )
    shared = DomainScheduler.from_settings(settings, global_limit=10, max_rps=0.0)

    both_running = asyncio.Barrier(2)

    async def slow_worker(job):
        async with both_running:
            return job

    jobs = [SharedJob(0), SharedJob(1)]

    async def scenario():
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(
                shared.run(jobs, slow_worker, failed_result=lambda j, e: None),
                timeout=0.2,
            )

    asyncio.run(scenario())
