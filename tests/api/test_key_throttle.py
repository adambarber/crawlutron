"""Key-verification metering (#653): the pre-auth PBKDF2 amplifier is throttled.

N consecutive failed verifies from the same presented key id trip a fail-closed
cooldown at the single chokepoint (get_caller_identity's wrapper), not per-door.
"""

from __future__ import annotations

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


def test_garbage_keys_share_one_unknown_bucket():
    """Unparseable credentials pool into the fixed 'unknown' bucket, so an
    attacker cannot mint unlimited buckets by varying garbage."""
    throttle = KeyVerifyThrottle()

    for i in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(f"garbage-{i}")

    with pytest.raises(Exception) as raised:
        throttle.check("garbage-fresh-never-seen")
    assert getattr(raised.value, "status_code", None) == 429


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


def _flood_failures(throttle: KeyVerifyThrottle, count: int) -> int:
    """Record one failure on `count` distinct parseable ids; return how many
    mint attempts were 429-refused."""
    refused = 0
    for i in range(count):
        try:
            throttle.record_failure(f"ck_{i:016x}.flood")
        except Exception as exc:
            _assert_429(exc)
            refused += 1
    return refused


def test_flood_cannot_evict_a_cooldown_victim():
    """#659: a distinct-id flood cannot launder a victim out of its cooldown --
    the mint budget caps bucket creation, so the victim's ledger survives."""
    throttle = KeyVerifyThrottle()
    victim = "ck_0123456789abcdef.victim"
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(victim)
    _refuses(throttle, victim)

    # Mint pricing refuses part of the flood; only 429s are legal.
    _flood_failures(throttle, 1100)
    _refuses(throttle, victim)


def test_bucket_minting_pays_against_a_global_rate():
    """#659: minting fresh buckets is metered against a global rate, so the
    flood itself is priced instead of consuming unbounded meter state."""
    throttle = KeyVerifyThrottle()
    assert _flood_failures(throttle, 1100) > 0, "a 1100-bucket flood minted every bucket unpriced"
    with pytest.raises(Exception) as raised:
        throttle.record_failure("ck_ffffffffffffffff.fresh")
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
        assert throttled.json()["detail"]["code"] == "key_verify_throttled"
        after = caller_key_store.pbkdf2_calls - calls_before
        assert after == KeyVerifyThrottle.MAX_FAILURES, f"burned {after} PBKDF2 calls"
