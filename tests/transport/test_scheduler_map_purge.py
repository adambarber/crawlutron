"""Idle scheduler state for retired domains is purged, not retained (#544/P9)."""

from crawlutron.transport.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler, TokenBucket
from tests._scheduler_doubles import make_scheduler


def _scheduler(**overrides) -> DomainScheduler:
    return make_scheduler(global_limit=4, per_domain_limit=2, delay_seconds=0.0, **overrides)


def test_purge_drops_latency_for_silent_domains():
    s = _scheduler()
    s.record_latency("live.test", 100.0)
    s.record_latency("retired.test", 200.0)
    # Retire: rewind its last-seen past the retention window; record_latency
    # set it, so the None arm of the union is dead here.
    retired = s._state("retired.test")
    assert retired.last_seen is not None
    retired.last_seen -= s._circuit_cooldown_max * 2
    s.purge_expired()
    assert "live.test" in s._domain_state
    assert "retired.test" not in s._domain_state


def test_purge_drops_token_buckets_without_live_presence():
    s = _scheduler()
    s.record_latency("live.test", 100.0)
    s.set_domain_cooldown("cooled.test", 60.0)
    # Buckets appear exactly as _fire_wait builds them: on first sight.
    s._state("live.test").bucket = TokenBucket(rate=1.0)
    s._state("cooled.test").bucket = TokenBucket(rate=1.0)
    s._state("retired.test").bucket = TokenBucket(rate=1.0)
    s.purge_expired()
    assert "retired.test" not in s._domain_state
    assert "cooled.test" in s._domain_state  # cooldown keeps it live
    assert "live.test" in s._domain_state  # fresh latency keeps it live


def test_amortized_purge_still_runs_on_set_domain_cooldown():
    s = _scheduler()
    s.record_latency("retired.test", 50.0)
    # Silenced long ago: the amortized cadence must still drop it.
    retired = s._state("retired.test")
    assert retired.last_seen is not None
    retired.last_seen -= s._circuit_cooldown_max * 2
    for _ in range(COOLDOWN_PURGE_INTERVAL):
        s.set_domain_cooldown("d.test", 1.0)
    assert "retired.test" not in s._domain_state


def test_in_flight_reservation_is_never_purged():
    """#577: a future fire time keeps the record alive through a mid-run purge.

    The herd re-formation the defect describes happens exactly when a 429
    cadence purge fires while peers hold reservations; a record carrying
    ``next_fire > now`` must survive it.
    """
    now = [1000.0]
    s = _scheduler(clock=lambda: now[0])
    s._state("spaced.test").next_fire = now[0] + 10.0
    s.purge_expired()
    assert "spaced.test" in s._domain_state, "a live reservation was purged mid-run"


def test_charged_failure_ladder_survives_the_purge():
    """#577: a domain failing below threshold (no cooldown, no circuit, no EMA)
    keeps its failure count -- purging it would silence quiet-but-failing
    domains' breakers on non-adaptive configs."""
    s = _scheduler(adaptive=False)
    s.record_failure("failing.test")
    s.purge_expired()
    assert "failing.test" in s._domain_state
    assert s._state("failing.test").failures == 1


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
    s = _scheduler(clock=lambda: now[0])
    s._state("dead.test").next_fire = now[0] - 1.0  # spent reservation
    s.purge_expired()
    assert "dead.test" not in s._domain_state
