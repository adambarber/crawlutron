"""The operator gate meters its negative guesses (#664/#659 parity).

The /metrics and /v1/policy-suggestions operator gates pace wrong-token guesses
through the SAME admin bucket /v1/keys uses: 5 misses, then a 60s cooldown
refusing 429 before any comparison work.
"""

from __future__ import annotations

from crawlutron.api.key_throttle import KeyVerifyThrottle, shared_throttle
from tests._app_factory import fresh_app
from tests._async_harnesses import serve


async def test_metrics_wrong_token_guesses_trip_the_cooldown():
    """MAX_FAILURES wrong tokens on /metrics pace the admin bucket; the next
    operator-gated guess is a 429 before comparison work (parity with /v1/keys)."""
    async with serve(fresh_app()) as client:
        for i in range(KeyVerifyThrottle.MAX_FAILURES):
            response = await client.get("/metrics", headers={"X-API-Key": f"wrong-{i}"})
            assert response.status_code == 200, response.text
        throttled = await client.get("/metrics", headers={"X-API-Key": "wrong-999"})
        assert throttled.status_code == 429, throttled.text
        assert throttled.json()["code"] == "key_verify_throttled"


async def test_metrics_exhausted_bucket_still_serves_base_exposition():
    """The cooldown paces operator DETAIL, not the base scrape: an anonymous
    /metrics stays 200 with base families only."""
    throttle = shared_throttle()
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_admin_failure()
    async with serve(fresh_app()) as client:
        response = await client.get("/metrics")
        assert response.status_code == 200, response.text
        assert "by_caller_total" not in response.text
