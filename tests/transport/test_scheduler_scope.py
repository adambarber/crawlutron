"""Scheduler scope honesty: per-domain state is per-instance, not process-wide."""

import asyncio
from dataclasses import dataclass

import pytest

import crawlutron.transport.scheduler as scheduler_mod
from crawlutron.config.settings import CrawlerSettings
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.taxonomy import ResultStatus
from tests._timing_harnesses import HANG_GUARD_SECONDS


@dataclass
class _Result:
    """The SchedulerResult shape every worker must hand back: status drives the
    domain-health verdict, elapsed_ms feeds the adaptive latency estimator."""

    status: ResultStatus = "success"
    elapsed_ms: int = 1


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


def _two_instances_with_one_job_each():
    """The shared scenario for the scope pair: two per-domain_limit=1 instances,
    one job on the same domain each. Returns (a, b, jobs_a, jobs_b)."""
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "1",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "0",
        }
    )
    a = DomainScheduler.from_settings(settings, global_limit=10, max_rps=0.0)
    b = DomainScheduler.from_settings(settings, global_limit=10, max_rps=0.0)
    jobs_a = [_Job(0)]
    jobs_b = [_Job(1)]
    return a, b, jobs_a, jobs_b


class _Job:
    """One queued job on the shared test domain."""

    def __init__(self, index):
        self.index = index
        self.domain = "example.com"


async def _race_both_instances(a, b, jobs_a, jobs_b, worker, timeout_seconds):
    """Run both instances' single job through ``worker`` under one timeout:
    the gather both scope tests share, parameterized so the two tests differ
    only in worker, timeout, and expectation."""
    return await asyncio.wait_for(
        asyncio.gather(
            a.run(jobs_a, worker, failed_result=lambda j, e: None),
            b.run(jobs_b, worker, failed_result=lambda j, e: None),
        ),
        timeout=timeout_seconds,
    )


async def test_concurrent_instances_allow_combined_concurrency():
    """Two schedulers with per_domain_limit=1 each allow 1 concurrent → combined 2.

    This demonstrates the scope: the per-domain gate is per-instance. A caller
    assuming a service-wide bound would observe double the rate with two requests.
    """
    a, b, jobs_a, jobs_b = _two_instances_with_one_job_each()

    # A rendezvous, not a stopwatch: the two workers must both be inside their
    # jobs at once, which only independent gates permit.
    both_running = asyncio.Barrier(2)
    success_a, success_b = _Result(), _Result()

    async def slow_worker(job):
        async with both_running:
            return success_a if job.index == 0 else success_b

    results = await _race_both_instances(a, b, jobs_a, jobs_b, slow_worker, HANG_GUARD_SECONDS)
    # run() returns job.index -> the worker's own result, not a failure fill:
    # each instance delivered exactly its worker's verdict for its own job.
    assert results[0] == {0: success_a}
    assert results[1] == {1: success_b}


async def test_a_shared_gate_mint_sinks_the_rendezvous(monkeypatch):
    """Literal mutation for the rendezvous above: minting ONE shared gate for
    both instances (the collapse #577 guards against) must break the rendezvous
    -- the second worker can never enter, so the run times out under the guard."""

    shared_gate = scheduler_mod.DomainGate(1)
    monkeypatch.setattr(scheduler_mod, "DomainGate", lambda limit: shared_gate)

    a, b, jobs_a, jobs_b = _two_instances_with_one_job_each()

    both_running = asyncio.Barrier(2)

    async def slow_worker(job):
        async with both_running:
            return _Result()

    with pytest.raises(asyncio.TimeoutError):
        await _race_both_instances(a, b, jobs_a, jobs_b, slow_worker, 0.2)
