"""Key-verification metering (#653): the pre-auth PBKDF2 amplifier is throttled.

N consecutive failed verifies from the same presented key id trip a fail-closed
cooldown at the single chokepoint (get_caller_identity's wrapper), not per-door.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from crawlutron.api.key_throttle import KeyVerifyThrottle
from crawlutron.storage.caller_key_store import CallerKeyStore


def test_consecutive_failed_verifies_trip_cooldown(tmp_path):
    """N consecutive misses from one presented key id refuse BEFORE the PBKDF2
    burns again; a hit path (different key id) is untouched."""
    store = CallerKeyStore(tmp_path / "keys.db")
    throttle = KeyVerifyThrottle()
    good = store.issue_key("agent-a")
    other = store.issue_key("agent-b")
    bad_id = "ck_0123456789abcdef.wrong-body"

    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        assert store.verify_key(bad_id) is None
        throttle.record_failure(bad_id)

    # The cooldown is fail-closed: even the LIVE secret for that id is refused.
    refused = store.verify_key(good.secret)
    assert refused is not None  # the store itself is not the meter
    with pytest.raises(Exception) as raised:
        throttle.check(bad_id)
    assert getattr(raised.value, "status_code", "") == 429

    # A different key id has its own bucket and stays clean.
    throttle.check(other.secret)


def test_throttle_is_per_entry_point_not_per_door():
    """The meter lives at the verification chokepoint: one instance answers for
    every auth-bearing door, keyed by the presented credential, not by route."""
    throttle = KeyVerifyThrottle()
    garbage = "not-even-a-key"

    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(garbage)

    # The same instance refuses on any door's behalf once the bucket is full.
    with pytest.raises(Exception) as raised:
        throttle.check(garbage)
    assert getattr(raised.value, "status_code", None) == 429


def test_successful_verify_resets_the_counter(tmp_path):
    """A hit clears the bucket: a caller who fat-fingers once then succeeds is
    not on a hair-trigger ladder."""
    store = CallerKeyStore(tmp_path / "keys.db")
    throttle = KeyVerifyThrottle()
    issued = store.issue_key("agent-a")

    for _ in range(KeyVerifyThrottle.MAX_FAILURES - 1):
        throttle.record_failure(issued.secret)
    assert store.verify_key(issued.secret) is not None
    throttle.record_success(issued.secret)
    # Zero failures left: one more miss does not trip the cooldown.
    throttle.record_failure(issued.secret)
    throttle.check(issued.secret)


def test_throttle_state_is_registered_process_state():
    """The meter is process state like every other holder (#621): it registers
    a reset/probe pair with the registry."""
    import crawlutron.api.key_throttle as key_throttle
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    assert process_state.owner_has_state("crawlutron.api.key_throttle") is False
    throttle = key_throttle.shared_throttle()
    throttle.record_failure("ck_0123456789abcdef.x")
    assert process_state.owner_has_state("crawlutron.api.key_throttle") is True


def _assert_429(exc: BaseException) -> None:
    """The meter's one refusal shape, as every #659 refusal reads it."""
    assert getattr(exc, "status_code", None) == 429, exc


def _refuses(throttle: KeyVerifyThrottle, presented: str) -> None:
    """The presented credential's next verify raises the 429 refusal."""
    with pytest.raises(Exception) as raised:
        throttle.check(presented)
    _assert_429(raised.value)


async def test_door_refuses_throttled_key_without_pbkdf2(tmp_path):
    """Over HTTP: N garbage keys from one bucket hit 401 PBKDF2 cost then 429;
    the meter refuses without running another PBKDF2."""
    from crawlutron.storage import caller_key_store
    from tests.factories.client import isolation_client

    garbage = "ck_0123456789abcdef.wrong-body"
    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        responses = []
        calls_before = caller_key_store.pbkdf2_calls
        for _ in range(KeyVerifyThrottle.MAX_FAILURES):
            responses.append(
                await client.post(
                    "/v1/crawl",
                    headers={"X-API-Key": garbage},
                    json={"url": "http://example.com/", "async_mode": True},
                )
            )
        assert all(r.status_code == 401 for r in responses), [r.status_code for r in responses]
        throttled = await client.post(
            "/v1/crawl",
            headers={"X-API-Key": garbage},
            json={"url": "http://example.com/", "async_mode": True},
        )
        assert throttled.status_code == 429, throttled.text
        assert throttled.json()["code"] == "key_verify_throttled"
        after = caller_key_store.pbkdf2_calls - calls_before
        assert after == KeyVerifyThrottle.MAX_FAILURES, f"burned {after} PBKDF2 calls"


def _frozen_clock():
    """An injected monotonic-shaped clock a test can advance."""
    state = {"now": 100.0}

    def clock() -> float:
        return state["now"]

    return state, clock


def test_cooldown_survives_a_capacity_flood():
    """#675: an active cooldown survives the LRU cap -- a flood cannot launder
    a cooldown victim's ledger (the surviving owner of the #663 retention pin,
    whose sweep mechanism was deleted with the mint budget)."""
    state, clock = _frozen_clock()
    throttle = KeyVerifyThrottle(clock=clock)
    victim = "ck_0123456789abcdef.victim"
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(victim)
    _refuses(throttle, victim)

    for i in range(1200):
        throttle.record_failure(f"ck_{i:016x}.flood-{i}")
    _refuses(throttle, victim)


def test_cooldown_expiry_rearms_the_bucket():
    """#661: after a cooldown elapses, ONE wrong guess must not re-trip an
    instant re-cooldown -- the expired cooldown clears the failure ladder, so
    the next MAX_FAILURES guesses are needed before cooling again."""
    state, clock = _frozen_clock()
    throttle = KeyVerifyThrottle(clock=clock)
    presented = "ck_0123456789abcdef.cooldown"

    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(presented)
    _refuses(throttle, presented)

    state["now"] += 61.0
    # The cooldown has expired; one wrong guess must NOT re-arm it...
    throttle.record_failure(presented)
    throttle.check(presented)
    # ...and the full ladder must survive: MAX_FAILURES-1 more guesses stay
    # admitted, the MAX_FAILURES-th re-cools.
    for _ in range(KeyVerifyThrottle.MAX_FAILURES - 2):
        throttle.record_failure(presented)
        throttle.check(presented)
    throttle.record_failure(presented)
    _refuses(throttle, presented)


class _RacyThrottle(KeyVerifyThrottle):
    """Pre-fix shape: bucket resolved under the lock, mutation outside it --
    the lost-update window #703 closed. A gate barrier makes every worker
    read before any worker stores; the lock is released first: no deadlock."""

    def __init__(self, *, clock, gate):
        super().__init__(clock=clock)
        self._read_gate = gate

    def _record_bucket_failure(self, key: str) -> None:
        # Pre-fix shape: resolve under the lock, then mutate outside it.
        with self._lock:
            bucket = self._locked_get_or_mint(key, self._clock())
        self._record_failure_into(bucket, self._clock())

    def _record_failure_into(self, bucket, now):
        current = bucket.failures
        # Rendezvous: every worker reads before any worker stores.
        self._read_gate.wait()
        bucket.failures = current + 1


def _run_concurrent(throttle: KeyVerifyThrottle, key: str, count: int) -> None:
    """Run `count` concurrent record_failure calls on `key`, asserting every
    worker completes without raising; the caller-provided throttle's gate
    drives the interleaving under test."""
    outcomes: list[Exception | None] = []

    def worker() -> None:
        try:
            throttle.record_failure(key)
        except Exception as exc:
            outcomes.append(exc)
        else:
            outcomes.append(None)

    with ThreadPoolExecutor(max_workers=count) as pool:
        [pool.submit(worker) for _ in range(count)]
    assert len(outcomes) == count, outcomes
    assert not any(outcomes), outcomes


def test_concurrent_record_failure_loses_increments_without_lock():
    """#703 bug class, deterministic: without the lock every worker reads
    failures == 0 and stores 1, so the counter ends at 1, never reaching
    MAX_FAILURES or cooling; no deadlock, no sleeps, no GIL timing."""
    count = KeyVerifyThrottle.MAX_FAILURES
    key = "ck_0123456789abcdef.racy"
    state, clock = _frozen_clock()
    gate = threading.Barrier(count)
    throttle = _RacyThrottle(clock=clock, gate=gate)

    _run_concurrent(throttle, key, count)

    # Barriered reads all see 0, so exactly one increment survives; no cooldown.
    throttle.check(key)


def test_concurrent_failures_reach_cooldown_threshold():
    """N concurrent failures on one credential serialize under the lock and
    reach the cooldown threshold; no increment is lost. The fix makes it."""
    count = KeyVerifyThrottle.MAX_FAILURES
    key = "ck_0123456789abcdef.concurrent"
    state, clock = _frozen_clock()
    throttle = KeyVerifyThrottle(clock=clock)

    _run_concurrent(throttle, key, count)

    # All N failures landed: bucket at threshold, fail-closed.
    with pytest.raises(Exception) as raised:
        throttle.check(key)
    assert getattr(raised.value, "status_code", None) == 429
