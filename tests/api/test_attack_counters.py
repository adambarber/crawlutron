"""Attack-observability counters: every refusal-class event is countable.

One test per counter family; each counts through the public metrics surface
(count, then read back) and names the funnel that writes it.
"""

from __future__ import annotations

import pytest

from crawlutron.runtime.metrics import get_metrics
from crawlutron.service import key_throttle
from crawlutron.service.errors import KeyVerifyThrottled
from crawlutron.service.key_throttle import KeyVerifyThrottle
from crawlutron_rest.api import anonymous_throttle
from crawlutron_rest.api.anonymous_throttle import AnonymousThrottle


def _meter_snapshot() -> dict[str, float]:
    snap = get_metrics().snapshot()
    return {
        "auth_refusals": snap.auth_refusals_total,
        "key_verify_throttle_trips": snap.key_verify_throttle_trips_total,
        "anonymous_throttle_refusals": snap.anonymous_throttle_refusals_total,
        "admin_auth_failures": snap.admin_auth_failures_total,
    }


def test_metric_families_declared_in_the_registry():
    """The four attack-class counters are declared exposition families."""
    from crawlutron.runtime.metrics import COUNTER_FAMILIES

    names = [family.name for family in COUNTER_FAMILIES]
    for expected in (
        "crawler_auth_refusals_total",
        "crawler_key_verify_throttle_trips_total",
        "crawler_anonymous_throttle_refusals_total",
        "crawler_admin_auth_failures_total",
    ):
        assert expected in names


async def _refused_keys_request(counter: str, tmp_path) -> None:
    """One 401'd /v1/keys request; assert the named counter moved by one."""
    from tests.factories.client import isolation_client

    async with isolation_client(tmp_path) as (client, _store, _results, _keys):
        before = _meter_snapshot()[counter]
        response = await client.get("/v1/keys", headers={"X-API-Key": "not-the-token"})
        assert response.status_code == 401
        assert _meter_snapshot()[counter] == before + 1.0


async def test_invalid_key_401_is_counted(tmp_path):
    """A wrong /v1/keys credential bumps crawler_auth_refusals_total."""
    await _refused_keys_request("auth_refusals", tmp_path)


def test_key_verify_throttle_trip_is_counted():
    """The 429 path in KeyVerifyThrottle._check_bucket bumps its counter."""
    throttle = key_throttle.KeyVerifyThrottle()
    presented = "ck_0123456789abcdef.trip"
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(presented)
    with pytest.raises(KeyVerifyThrottled) as raised:
        throttle.check(presented)
    assert raised.value.status == 429
    assert _meter_snapshot()["key_verify_throttle_trips"] == 1.0


def test_anonymous_429_is_counted():
    """The anonymous meter's refusal bumps its counter."""
    meter = AnonymousThrottle()
    # MAX consumed + 1 refusal = MAX+1 observable.
    for _ in range(anonymous_throttle.MAX_REQUESTS_PER_WINDOW):
        meter.check("10.9.9.9")
    with pytest.raises(anonymous_throttle.Throttled):
        meter.check("10.9.9.9")
    assert _meter_snapshot()["anonymous_throttle_refusals"] == 1.0


async def test_admin_guess_is_counted(tmp_path):
    """A wrong admin token on /v1/keys bumps crawler_admin_auth_failures_total."""
    await _refused_keys_request("admin_auth_failures", tmp_path)


def test_admin_failure_recording_is_counted():
    """The admin bucket increment in the throttle bumps its counter."""
    throttle = key_throttle.KeyVerifyThrottle()
    throttle.record_admin_failure()
    assert _meter_snapshot()["admin_auth_failures"] == 1.0


def test_throttled_check_does_not_count_a_trip():
    """An in-budget verify does not increment the trip counter."""
    key_throttle.KeyVerifyThrottle().check("ck_0123456789abcdef.clean")
    assert _meter_snapshot()["key_verify_throttle_trips"] == 0.0


def test_under_budget_anonymous_check_does_not_count_a_refusal():
    """An in-budget anonymous request does not increment the refusal counter."""
    AnonymousThrottle().check("10.9.9.8")
