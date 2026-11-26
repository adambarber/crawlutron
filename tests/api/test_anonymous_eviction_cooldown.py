"""Anonymous bucket eviction must never destroy a live key-verify cooldown (#1481)."""

from __future__ import annotations

import pytest

from crawlutron.service.errors import KeyVerifyThrottled
from crawlutron_rest.api.throttle import ANON_MAX_REQUESTS, Throttle, Throttled
from tests._clocks import arrange_clock

VICTIM = "ck_0123456789abcdef.victim"
_FLOODS = (1023, 1024, 1025, 2048, 2100)


def _arm_cooldown(throttle: Throttle, key: str = VICTIM) -> None:
    """Trip the key-verify ladder; the bucket is the oldest entry at flood time."""
    for _ in range(Throttle.MAX_FAILURES):
        throttle.record_failure(key)


def _assert_cooling(throttle: Throttle, key: str = VICTIM) -> None:
    """The cooldown is still armed: the next verify refuses."""
    with pytest.raises(KeyVerifyThrottled):
        throttle.check(key)


def _flood(throttle: Throttle, count: int) -> None:
    """Issue anonymous requests from distinct IPs, each a fresh bucket."""
    for i in range(count):
        throttle.check_anon(f"198.51.100.{i // 254}.{(i % 254) + 1}")


@pytest.mark.parametrize("flood", _FLOODS, ids=[f"anon-{n}" for n in _FLOODS])
def test_cooldown_survives_anonymous_eviction_flood(flood: int) -> None:
    """Kills mutant: shared _buckets with an unguarded popitem(last=False) eviction."""
    _state, clock = arrange_clock(start=100.0)
    throttle = Throttle(clock=clock)
    _arm_cooldown(throttle)
    _flood(throttle, flood)
    _assert_cooling(throttle)


def test_cooldown_survives_interleaved_traffic() -> None:
    """Kills mutant: a type check on only one eviction path (anon mint before key)."""
    _state, clock = arrange_clock(start=100.0)
    throttle = Throttle(clock=clock)
    _flood(throttle, 512)
    _arm_cooldown(throttle)
    _flood(throttle, 1024)
    _assert_cooling(throttle)
    _flood(throttle, 700)
    _assert_cooling(throttle)


def test_anonymous_budget_holds_after_the_cooldown_flood() -> None:
    """Kills mutant: splitting state but charging the anonymous budget to the key map."""
    _state, clock = arrange_clock(start=100.0)
    throttle = Throttle(clock=clock)
    _arm_cooldown(throttle)
    _flood(throttle, 2100)
    remote = "203.0.113.77"
    for _ in range(ANON_MAX_REQUESTS):
        throttle.check_anon(remote)
    with pytest.raises(Throttled):
        throttle.check_anon(remote)
    _assert_cooling(throttle)


def test_shared_store_cooldown_survives_anonymous_flood(tmp_path) -> None:
    """Kills mutant: a shared-store namespace collision between the two ladders."""
    _state, clock = arrange_clock(start=100.0)
    throttle = Throttle(clock=clock, shared_db_path=tmp_path / "throttle.sqlite3")
    try:
        _arm_cooldown(throttle)
        _flood(throttle, 2100)
        _assert_cooling(throttle)
    finally:
        throttle.unbind_shared_db()
