"""Same-domain requests are SPACED, not staggered once: the politeness contract."""

import pytest

from crawlutron.transport.scheduler import DomainScheduler
from tests._result_builders import make_job
from tests._scheduler_doubles import engine_envelope, make_scheduler, ok_worker
from tests._timing_harnesses import recorder

# The jitter band (0.8..1.2 of the base) bounds every spacing sleep a batch requests.
_JITTER_BAND = (0.07, 0.13)


def _frozen_scheduler(of=DomainScheduler, **overrides) -> DomainScheduler:
    """A scheduler on a frozen clock: recorded sleeps equal their jitter draw."""
    return make_scheduler(of, clock=lambda: 0.0, **overrides)


def _two_jobs(url_a: str, url_b: str):
    return [make_job(0, url_a), make_job(1, url_b)]


class TestSameDomainSpacing:
    async def test_second_same_domain_job_waits_for_the_first_fire(self, monkeypatch):
        """Two same-domain jobs with a configured delay must not fire together.

        The first fire waits nothing; the second waits one jittered delay past
        it -- the requested waits are staggered, never two copies of the delay.
        """
        sleeps = recorder(monkeypatch)
        scheduler = _frozen_scheduler(delay_seconds=0.1, adaptive=False)
        jobs = _two_jobs("http://example.com/a", "http://example.com/b")
        results = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert results is not None and sorted(results) == [0, 1]
        spacing = sleeps.in_range(*_JITTER_BAND)
        assert len(spacing) == 1, (
            f"same-domain fires must be spaced by exactly one delay, saw {sleeps.delays}"
        )

    async def test_committed_crawl_delay_spaces_successive_requests(self, monkeypatch):
        """A robots Crawl-delay committed at admission paces the whole batch.

        The floor flows through the public commit path; the second request on
        the domain waits the floor past the first, not concurrently with it.
        """
        sleeps = recorder(monkeypatch)
        scheduler = _frozen_scheduler(delay_seconds=0.0, adaptive=False)
        scheduler.set_domain_min_delay("example.com", 0.5)
        jobs = _two_jobs("http://example.com/a", "http://example.com/b")
        await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        spacing = sleeps.in_range(0.4, 0.6)
        assert len(spacing) == 1, (
            f"the committed Crawl-delay must space successive fires, saw {sleeps.delays}"
        )

    async def test_different_domains_are_never_serialized_by_each_other(self, monkeypatch):
        """Registrable-distinct domains with different delays owe each other nothing: two same-domain jobs
        PER domain, magnitude-separated, and each domain's second fire waits its own delay -- a
        cross-domain-serialized scheme would push the fast domain into the slow band instead.
        """

        class Separated(DomainScheduler):
            def current_delay(self, domain: str) -> float:
                return 1.0 if domain == "example.com" else 0.05

        sleeps = recorder(monkeypatch)
        scheduler = _frozen_scheduler(Separated, adaptive=False)
        jobs = [
            make_job(0, "http://a.example.com/1"),
            make_job(1, "http://a.example.com/2"),
            make_job(2, "http://b.example.org/1"),
            make_job(3, "http://b.example.org/2"),
        ]
        results = await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert results is not None and sorted(results) == [0, 1, 2, 3]
        # Each domain's SECOND fire carries exactly its own delay; neither waits the other's.
        fast = sleeps.in_range(0.03, 0.07)
        slow = sleeps.in_range(0.7, 1.3)
        assert len(fast) == 1 and len(slow) == 1, (
            "each domain's second fire must wait only its own delay; cross-domain "
            f"serialization would push the fast domain into the slow band: {sleeps.delays}"
        )

    async def test_spacing_preserves_per_domain_concurrency(self, monkeypatch):
        """Three same-domain jobs at limit 2: staggered reservations, capped workers. Fires are staggered
        cumulatively by the delay and the gate still caps workers at the limit -- a gate-held-for-sleep
        scheme serializes to one-at-a-time, a no-reservation scheme fires all three at once.
        """
        sleeps = recorder(monkeypatch)
        scheduler = _frozen_scheduler(global_limit=2, per_domain_limit=2, delay_seconds=0.1, adaptive=False)
        in_flight = 0
        peak = 0

        async def counting_worker(job):
            nonlocal in_flight, peak
            in_flight += 1
            peak = max(peak, in_flight)
            result = await ok_worker(job)
            in_flight -= 1
            return result

        jobs = [make_job(i, f"http://example.com/{i}") for i in range(3)]
        results = await scheduler.run(jobs, counting_worker, failed_result=engine_envelope)
        assert results is not None and sorted(results) == [0, 1, 2]
        assert peak <= 2, f"per_domain_limit must cap concurrent workers, saw peak {peak}"
        spacing = sorted(sleeps.in_range(0.06, 0.4))
        # The gate is never held for the sleep, so at least two fires reserve
        # concurrently; cumulative spacing means the last waits ~two delays.
        assert len(spacing) >= 2 and spacing[-1] >= 0.16, (
            f"fires must stagger cumulatively by the delay, saw {sleeps.delays}"
        )


class TestZeroDelayNeverInventsPoliteness:
    async def test_zero_delay_domain_still_requests_no_wait(self, monkeypatch):
        """No configured delay and no committed floor: zero sleep requests only.

        A reservation scheme must not mint waits for domains that owe none --
        the no-op pacing path depends on zero-delay runs completing instantly.
        """
        sleeps = recorder(monkeypatch)
        scheduler = make_scheduler(adaptive=False)
        jobs = _two_jobs("https://example.com/a", "https://example.com/b")
        await scheduler.run(jobs, ok_worker, failed_result=engine_envelope)
        assert not [d for d in sleeps.delays if d > 0], (
            f"a zero-delay domain must request no positive wait, saw {sleeps.delays}"
        )


class TestMidRunPurgeKeepsState:
    async def test_a_cooldown_firing_mid_run_does_not_void_spacing_state(self, monkeypatch):
        """The amortized purge (429 cadence) must not erase a live reservation: a same-domain peer taking a
        429 mid-run purges, and the sleeping job's spacing must survive it, or the herd re-forms exactly
        under rate limiting -- the one scenario politeness exists for.
        """
        sleeps = recorder(monkeypatch)
        scheduler = _frozen_scheduler(delay_seconds=0.1, adaptive=False)
        jobs = [make_job(i, f"http://example.com/{i}") for i in range(4)]
        # The purge cadence fires from set_domain_cooldown; run one mid-batch.

        async def worker(job):
            if job.index == 2:
                scheduler.set_domain_cooldown("other.test", 5.0)
                scheduler.purge_expired()
            return await ok_worker(job)

        await scheduler.run(jobs, worker, failed_result=engine_envelope)
        first_band = sleeps.in_range(*_JITTER_BAND)
        assert len(first_band) == 1, (
            f"a mid-run purge must not reset the domain's fire schedule: "
            f"{len(first_band)} jobs re-queued at the initial delay of {sleeps.delays}"
        )
        # The committed floor outlives the purge too: it is never time-elapsed state.
        assert scheduler.current_delay("example.com") == pytest.approx(0.1)
