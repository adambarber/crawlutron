"""Idle scheduler state for retired domains is purged, not retained (#544/P9)."""

import asyncio

from crawlutron.transport.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler, TokenBucket
from tests._result_builders import make_job
from tests._scheduler_doubles import make_scheduler


def _scheduler(**overrides) -> DomainScheduler:
    knobs = {"global_limit": 4, "per_domain_limit": 2, "delay_seconds": 0.0}
    return make_scheduler(**{**knobs, **overrides})


def test_purge_drops_latency_for_silent_domains():
    now = [1000.0]
    s = _scheduler(clock=lambda: now[0])
    s.record_latency("live.test", 100.0)
    s.record_latency("retired.test", 200.0)
    # Retire: run the clock past the retention window, then re-stamp the live domain
    # so the window is measured from ITS last observation, not the retired one's.
    now[0] += s._circuit_cooldown_max * 2
    s.record_latency("live.test", 100.0)
    s.purge_expired()
    assert "live.test" in s.snapshot()
    assert "retired.test" not in s.snapshot()


def test_purge_drops_token_buckets_without_live_presence():
    now = [1000.0]
    # A short latency window so the cooldown outlives it: cooling for 60s while the
    # latency/bucket records go stale well before that expiry.
    s = _scheduler(max_rps=1.0, clock=lambda: now[0], circuit_cooldown_base=5.0, circuit_cooldown=10.0)
    s.record_latency("live.test", 100.0)
    s.set_domain_cooldown("cooled.test", 60.0)
    # Buckets appear exactly as _fire_wait builds them: on first sight. Drive the
    # real minting path so retired.test's bucket exists with nothing else live.
    asyncio.run(s._fire_wait(make_job(0, "http://retired.test/")))
    assert isinstance(s.snapshot()["retired.test"].bucket, TokenBucket)
    # Retire: past the latency window the bucket/latency records are inert; the
    # cooldown still stands, so cooled.test alone survives with retired.test dropped.
    now[0] += 20.0
    s.record_latency("live.test", 100.0)
    s.purge_expired()
    assert "retired.test" not in s.snapshot()
    assert "cooled.test" in s.snapshot()  # cooldown keeps it live
    assert "live.test" in s.snapshot()  # fresh latency keeps it live


def test_amortized_purge_still_runs_on_set_domain_cooldown():
    now = [1000.0]
    s = _scheduler(clock=lambda: now[0])
    s.record_latency("retired.test", 50.0)
    # Silenced long ago: the amortized cadence must still drop it.
    now[0] += s._circuit_cooldown_max * 2
    for _ in range(COOLDOWN_PURGE_INTERVAL):
        s.set_domain_cooldown("d.test", 1.0)
    assert "retired.test" not in s.snapshot()


def test_in_flight_reservation_is_never_purged():
    """#577: a future fire time keeps the record alive through a mid-run purge.

    The herd re-formation the defect describes happens exactly when a 429
    cadence purge fires while peers hold reservations; a record carrying
    ``next_fire > now`` must survive it.
    """
    now = [1000.0]
    s = _scheduler(clock=lambda: now[0], delay_seconds=10.0)
    # A real job reserves its fire time the way _fire_wait does: through the door.
    job = make_job(0, "http://spaced.test/")
    asyncio.run(s._fire_wait(job))
    assert s.snapshot()["spaced.test"].next_fire is not None
    s.purge_expired()
    assert "spaced.test" in s.snapshot(), "a live reservation was purged mid-run"


def test_charged_failure_ladder_survives_the_purge():
    """#577: a domain failing below threshold (no cooldown, no circuit, no EMA)
    keeps its failure count -- purging it would silence quiet-but-failing
    domains' breakers on non-adaptive configs."""
    s = _scheduler(adaptive=False)
    s.record_failure("failing.test")
    s.purge_expired()
    assert "failing.test" in s.snapshot()
    assert s.snapshot()["failing.test"].failures == 1


def test_committed_politeness_floor_survives_the_purge():
    """#577: the robots Crawl-delay floor is committed once (request_pipeline
    :347) and consumed across repeated run() calls, each purging up front --
    it must never be time-elapsed state."""
    s = _scheduler(adaptive=False)
    s.set_domain_min_delay("delayed.test", 5.0)
    s.purge_expired()
    assert s.current_delay("delayed.test") == 5.0, "the floor was purged"


def test_a_fully_inert_record_is_dropped():
    """The collapse's purpose: every field inert -> the record goes, including
    the retired domain's gate/bucket weight."""
    now = [1000.0]
    s = _scheduler(clock=lambda: now[0], delay_seconds=10.0)
    job = make_job(0, "http://dead.test/")
    asyncio.run(s._fire_wait(job))
    assert s.snapshot()["dead.test"].next_fire is not None
    now[0] += 20.0  # the reservation is spent and the record owes nothing else
    s.purge_expired()
    assert "dead.test" not in s.snapshot()
