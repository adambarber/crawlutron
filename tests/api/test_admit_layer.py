"""The /v1/admit layer inheritance (#664): the robots arm is metered. The route inherits caller identity +
the request gate from the layer, so a credential in key-verify cooldown refuses 429 before any outbound
fetch; the pure base check keeps its I/O-free, anonymous-capable contract.
"""

from __future__ import annotations

from fastapi import FastAPI

from crawlutron.service.key_throttle import KeyVerifyThrottle, shared_throttle
from tests._app_factory import fresh_app
from tests._async_harnesses import serve

#: A well-formed secret whose parseable key id mints its own throttle bucket.
_SECRET = "ck_deadbeefdeadbeef.testbody"


def _layered_admit_app() -> FastAPI:
    """The real app: the layer lives on the router, not in a test double."""
    return fresh_app()


async def test_the_pure_arm_stays_anonymous_and_unchanged():
    """check_robots=false stays I/O-free and anonymous-capable over the real app."""
    async with serve(_layered_admit_app()) as client:
        response = await client.post("/v1/admit", json={"url": "https://Example.com:443/a?b=1"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["admitted"] is True
    assert body["canonical_url"] == "https://example.com/a?b=1"


async def test_the_robots_arm_refuses_an_exhausted_caller():
    """check_robots=true under a credential in key-verify cooldown refuses 429.

    The meter runs BEFORE the fetch: an exhausted caller never drives an
    outbound robots request to an attacker-chosen host.
    """
    throttle = shared_throttle()
    for _ in range(KeyVerifyThrottle.MAX_FAILURES):
        throttle.record_failure(_SECRET)
    async with serve(_layered_admit_app()) as client:
        response = await client.post(
            "/v1/admit",
            headers={"X-API-Key": _SECRET},
            json={"url": "https://attacker.test/x", "check_robots": True},
        )
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "key_verify_throttled"


async def test_the_robots_arm_meters_a_live_caller_without_refusing(monkeypatch):
    """The robots arm runs under the layer: identity resolves (anonymous here),
    and the loader is the shared one, so the fetch rides the process TTL cache."""
    from crawlutron.service import admit_endpoint
    from crawlutron.transport.robots import RobotsRules, load_robots_rules
    from crawlutron.vocabulary.robots import RobotsSource

    assert admit_endpoint.load_robots_rules is load_robots_rules

    async def _fake_loader(target, fetcher, timeout_seconds, *, fetch_and_decode=None, policy=None):
        return RobotsRules([], [], None, RobotsSource.policy)

    monkeypatch.setattr("crawlutron.service.admit_endpoint.load_robots_rules", _fake_loader)
    async with serve(_layered_admit_app()) as client:
        response = await client.post(
            "/v1/admit",
            json={"url": "https://example.com/x", "check_robots": True},
        )
    body = response.json()
    assert response.status_code == 200, response.text
    assert body["checks"]["robots"] is None, body
