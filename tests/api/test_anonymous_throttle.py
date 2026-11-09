"""Anonymous IP-bucket metering (#668): the rate-limit law over the unguarded
surfaces -- the docs GETs, /mcp anonymous traffic, and the 404 route-miss."""

from __future__ import annotations

import httpx
import pytest

from crawlutron.api.anonymous_throttle import (
    BUCKET_CAPACITY,
    MAX_REQUESTS_PER_WINDOW,
    AnonymousThrottle,
    shared_anonymous_throttle,
)


def _clock(start: list[float]):
    def clock() -> float:
        return start[0]

    return clock


def _burst(throttle: AnonymousThrottle, remote: str | None, count: int) -> int:
    refused = 0
    for _ in range(count):
        try:
            throttle.check(remote)
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 429, exc
            refused += 1
    return refused


def test_burst_over_budget_refuses_under_budget_passes():
    """The budget is a real ceiling: MAX requests pass, the next one refuses."""
    now = [0.0]
    throttle = AnonymousThrottle(clock=_clock(now))
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, "10.0.0.1", 1) == 1


def test_window_rollover_restores_the_budget():
    """A lapsed window resets the bucket: the caller gets a fresh budget."""
    now = [0.0]
    throttle = AnonymousThrottle(clock=_clock(now))
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW + 5) == 5
    now[0] = 61.0
    assert _burst(throttle, "10.0.0.1", 1) == 0


def test_per_ip_isolation_two_ips_do_not_share_a_bucket():
    """One IP exhausting its budget leaves another IP's budget untouched."""
    throttle = AnonymousThrottle(clock=_clock([0.0]))
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, "10.0.0.1", 1) == 1
    assert _burst(throttle, "10.0.0.2", 1) == 0


def test_no_client_ip_shares_one_fixed_bucket():
    """Transports providing no remote address pool into one fixed bucket, so
    varying nothing cannot mint unlimited buckets."""
    throttle = AnonymousThrottle(clock=_clock([0.0]))
    assert _burst(throttle, None, MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, None, 1) == 1


def test_lru_eviction_is_bounded_and_never_resets_an_active_ip():
    """Attacker IP rotation beyond the capacity evicts the QUIESCENT victim:
    memory is bounded at capacity, and an ip touched DURING the flood (MRU
    end on every touch) keeps its accumulated ledger -- its next touch still
    refuses past budget, where a reset ledger would admit it."""
    now = [0.0]
    throttle = AnonymousThrottle(clock=_clock(now))
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    for i in range(BUCKET_CAPACITY):
        throttle.check(f"10.1.{i // 256}.{i % 256}")
        with pytest.raises(Exception) as raised:
            throttle.check("10.0.0.1")
        assert getattr(raised.value, "status_code", None) == 429
    assert throttle.residence() <= BUCKET_CAPACITY


def test_residence_is_the_probe_door():
    """The meter exposes its live bucket count (the #621 has/reset walk)."""
    throttle = AnonymousThrottle(clock=_clock([0.0]))
    assert throttle.residence() == 0
    throttle.check("10.0.0.1")
    assert throttle.residence() == 1


def test_meters_are_registered_process_state():
    """The anonymous meter registers a reset/probe pair with the registry."""
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    assert process_state.owner_has_state("crawlutron.api.anonymous_throttle") is False
    shared_anonymous_throttle().check("10.0.0.1")
    assert process_state.owner_has_state("crawlutron.api.anonymous_throttle") is True


def _app_client() -> httpx.AsyncClient:
    from tests._app_factory import service_app

    transport = httpx.ASGITransport(app=service_app())
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_docs_served_and_metered():
    """/docs answers within budget and 429s past it -- "it's just static docs"
    is not an exemption (the rate-limit law)."""
    process_state_reset()
    async with _app_client() as client:
        ok = await client.get("/docs")
        assert ok.status_code == 200
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.get("/redoc")
        refused = await client.get("/docs")
        assert refused.status_code == 429
        body = refused.json()
        assert body["code"] == "anonymous_throttled", body


async def test_mcp_anonymous_metered():
    """/mcp anonymous traffic meters through the IP bucket (JSON-RPC refusal);
    a presented credential skips the anonymous bucket (the #664 layer meters
    it through the caller's own quota)."""
    process_state_reset()
    async with _app_client() as client:
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        refused = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert refused.status_code == 429
        assert refused.json()["error"]["code"] == -32029


def process_state_reset() -> None:
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()


async def test_404_route_miss_is_metered_and_carries_both_discovery_links():
    """The 404 oracle is metered: the second burst past the anonymous budget
    429s, and within budget the details carry BOTH discovery doors."""
    process_state_reset()
    async with _app_client() as client:
        first = await client.get("/v1/definitely-not-a-route")
        assert first.status_code == 404
        assert first.json()["details"] == {
            "capabilities": "/.well-known/crawler-service",
            "docs": "/docs",
        }
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.get("/v1/definitely-not-a-route")
        refused = await client.get("/v1/definitely-not-a-route")
        assert refused.status_code == 429
        assert refused.json()["code"] == "anonymous_throttled"


async def test_conforming_404s_keep_their_exact_shape():
    """Unknown RESOURCE 404s (job/key) keep their existing envelope byte-shape;
    only the route-miss evolved."""
    process_state_reset()
    async with _app_client() as client:
        resp = await client.get("/v1/jobs/some-other-unknown-job")
        assert resp.status_code == 404
        assert resp.json()["code"] == "not_found"
        assert resp.json()["details"] == {}


async def test_405_is_not_anonymously_metered_shape_change():
    """A 405 on a known route keeps its exact existing envelope shape."""
    process_state_reset()
    async with _app_client() as client:
        resp = await client.post("/health")
        assert resp.status_code == 405
        assert resp.json()["details"] == {"status_code": 405}


@pytest.mark.parametrize(
    "path",
    ["/docs", "/redoc", "/docs/oauth2-redirect"],
)
async def test_every_docs_surface_is_metered(path):
    """Each static docs surface answers within budget and refuses past it."""
    process_state_reset()
    async with _app_client() as client:
        first = await client.get(path)
        assert first.status_code == 200
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.get(path)
        assert (await client.get(path)).status_code == 429
