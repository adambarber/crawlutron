"""RED evidence for #675 item 1: full-secret hashing with an LRU cap."""

from __future__ import annotations

import pytest

from crawlutron.service.errors import KeyVerifyThrottled
from crawlutron.service.key_throttle import KeyVerifyThrottle
from tests._clocks import advance_clock, arrange_clock


def test_red_garbage_under_victim_id_does_not_poison_victims_bucket():
    """Owner ruling 2026-11-09: hash FULL secret -- garbage bodies under a
    victim's public key id must cool their OWN bucket, never the victim's."""
    throttle = KeyVerifyThrottle()
    victim = "ck_0123456789abcdef.correct-body"

    for i in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(f"ck_0123456789abcdef.garbage-{i}")

    # The victim's real credential still admits (no cooldown raised).
    throttle.check(victim)


def test_red_flood_is_bounded_by_capacity_never_unbounded():
    """Replacing the mint budget with an LRU cap: a distinct-garbage flood's
    residence is bounded by the ledger capacity, and minting is never 429."""
    throttle = KeyVerifyThrottle()
    for i in range(1100):
        throttle.record_failure(f"ck_{i:016x}.flood-{i}")
    assert throttle.residence() <= 1024


def test_red_cooling_victim_survives_capacity_flood():
    """An ACTIVE cooldown victim is touched on every check (MRU refresh), so a
    capacity flood of quiescent garbage cannot evict its ledger."""
    throttle = KeyVerifyThrottle()
    victim = "ck_0123456789abcdef.victim"
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(victim)
    for i in range(1200):
        throttle.record_failure(f"ck_{i:016x}.flood-{i}")
    with pytest.raises(KeyVerifyThrottled) as raised:
        throttle.check(victim)
    assert raised.value.status == 429


def test_red_quiescent_garbage_evicts_before_an_active_victim():
    """The eviction scan skips cooling buckets: at full capacity of cooling
    buckets plus one fresh mint, the fresh non-cooling bucket is dropped, not
    a cooling victim's."""
    state, clock = arrange_clock(start=100.0)
    throttle = KeyVerifyThrottle(clock=clock)
    cooling = f"ck_{0:016x}.cooling"
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(cooling)
    advance_clock(state, 1.0)
    fresh = "ck_ffffffffffffffff.fresh"
    throttle.record_failure(fresh)
    throttle.check(fresh)
    # Reaching capacity of non-cooling buckets: eviction must pick the fresh
    # quiescent bucket, never the cooling one.
    for i in range(1023):
        throttle.record_failure(f"ck_{i + 1:016x}.flood-{i}")
    assert throttle.residence() == 1024
    with pytest.raises(KeyVerifyThrottled) as raised:
        throttle.check(cooling)
    assert raised.value.status == 429
    # The fresh bucket was the eviction victim (re-record needs full ladder).
    throttle.record_failure(fresh)
    throttle.check(fresh)
