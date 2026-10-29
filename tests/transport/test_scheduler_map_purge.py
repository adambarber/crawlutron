"""Idle scheduler state for retired domains is purged, not retained (#544/P9)."""

from crawlutron.transport.scheduler import COOLDOWN_PURGE_INTERVAL, DomainScheduler, TokenBucket


def _scheduler() -> DomainScheduler:
    return DomainScheduler(global_limit=4, per_domain_limit=2, delay_seconds=0.0)


def test_purge_drops_latency_for_silent_domains():
    s = _scheduler()
    s.record_latency("live.test", 100.0)
    s.record_latency("retired.test", 200.0)
    # Retire: rewind its last-seen past the retention window.
    s._domain_last_seen["retired.test"] -= s._circuit_cooldown_max * 2
    s.purge_expired()
    assert "live.test" in s._domain_latency_ema
    assert "retired.test" not in s._domain_latency_ema
    assert "retired.test" not in s._domain_last_seen


def test_purge_drops_token_buckets_without_live_presence():
    s = _scheduler()
    s.record_latency("live.test", 100.0)
    s.set_domain_cooldown("cooled.test", 60.0)
    # Buckets appear exactly as run_one builds them: on first sight.
    s._token_buckets["live.test"] = TokenBucket(rate=1.0)
    s._token_buckets["cooled.test"] = TokenBucket(rate=1.0)
    s._token_buckets["retired.test"] = TokenBucket(rate=1.0)
    s.purge_expired()
    assert "retired.test" not in s._token_buckets
    assert "cooled.test" in s._token_buckets  # cooldown keeps it live
    assert "live.test" in s._token_buckets  # fresh latency keeps it live


def test_amortized_purge_still_runs_on_set_domain_cooldown():
    s = _scheduler()
    s.record_latency("retired.test", 50.0)
    # Silenced long ago: the amortized cadence must still drop it.
    s._domain_last_seen["retired.test"] -= s._circuit_cooldown_max * 2
    for _ in range(COOLDOWN_PURGE_INTERVAL):
        s.set_domain_cooldown("d.test", 1.0)
    assert "retired.test" not in s._domain_latency_ema
