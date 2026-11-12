"""Anonymous IP-bucket metering (#668): the rate-limit law over the unguarded surfaces -- every ex-exempt
GET, the docs GETs, /mcp anonymous traffic, the route-miss oracle, and any response with no matched route
-- one request-stage unit each.
"""

from __future__ import annotations

import httpx
import pytest

from crawlutron_rest.api.anonymous_throttle import (
    BUCKET_CAPACITY,
    MAX_REQUESTS_PER_WINDOW,
    AnonymousThrottle,
    Throttled,
    shared_anonymous_throttle,
)
from tests._clocks import ClockState, arrange_clock


def _burst(throttle: AnonymousThrottle, remote: str | None, count: int) -> int:
    refused = 0
    for _ in range(count):
        try:
            throttle.check(remote)
        except Throttled:
            refused += 1
    return refused


def _throttle_on_arranged_time(start: float = 0.0) -> tuple[ClockState, AnonymousThrottle]:
    """An :class:`AnonymousThrottle` over an arrange clock the caller advances."""
    state, clock = arrange_clock(start=start)
    return state, AnonymousThrottle(clock=clock)


def test_burst_over_budget_refuses_under_budget_passes():
    """The budget is a real ceiling: MAX requests pass, the next one refuses."""
    _state, throttle = _throttle_on_arranged_time()
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, "10.0.0.1", 1) == 1


def test_window_rollover_restores_the_budget():
    """A lapsed window resets the bucket: the caller gets a fresh budget."""
    state, throttle = _throttle_on_arranged_time()
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW + 5) == 5
    state["now"] = 61.0
    assert _burst(throttle, "10.0.0.1", 1) == 0


def test_per_ip_isolation_two_ips_do_not_share_a_bucket():
    """One IP exhausting its budget leaves another IP's budget untouched."""
    _state, throttle = _throttle_on_arranged_time()
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, "10.0.0.2", 1) == 0


def test_no_client_ip_shares_one_fixed_bucket():
    """Transports providing no remote address pool into one fixed bucket, so
    varying nothing cannot mint unlimited buckets."""
    _state, throttle = _throttle_on_arranged_time()
    assert _burst(throttle, None, MAX_REQUESTS_PER_WINDOW) == 0
    assert _burst(throttle, None, 1) == 1


def test_lru_eviction_is_bounded_and_never_resets_an_active_ip():
    """Attacker IP rotation beyond capacity evicts the QUIESCENT victim: memory is bounded at capacity, and
    an ip touched DURING the flood (MRU on every touch) keeps its accumulated ledger -- its next touch
    still refuses past budget, where a reset ledger would admit it.
    """
    _state, throttle = _throttle_on_arranged_time()
    assert _burst(throttle, "10.0.0.1", MAX_REQUESTS_PER_WINDOW) == 0
    for i in range(BUCKET_CAPACITY):
        throttle.check(f"10.1.{i // 256}.{i % 256}")
        with pytest.raises(Throttled) as raised:
            throttle.check("10.0.0.1")
        assert raised.value.refusal_status == 429
    assert throttle.residence() <= BUCKET_CAPACITY


def test_residence_is_the_probe_door():
    """The meter exposes its live bucket count (the #621 has/reset walk)."""
    _state, throttle = _throttle_on_arranged_time()
    assert throttle.residence() == 0
    throttle.check("10.0.0.1")
    assert throttle.residence() == 1


def test_meters_are_registered_process_state():
    """The anonymous meter registers a reset/probe pair with the registry."""
    from crawlutron.runtime import process_state

    process_state.reset_all_process_state()
    assert process_state.owner_has_state("crawlutron_rest.api.anonymous_throttle") is False
    shared_anonymous_throttle().check("10.0.0.1")
    assert process_state.owner_has_state("crawlutron_rest.api.anonymous_throttle") is True


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


async def _assert_next_unit_refuses(client: httpx.AsyncClient) -> None:
    """The one-unit probe tail: the next request refuses with the envelope."""
    refused = await client.get("/health")
    assert refused.status_code == 429, "the rejection must have consumed one unit"
    assert refused.json()["code"] == "anonymous_throttled"


async def _fill_baseline(client: httpx.AsyncClient) -> None:
    """Consume MAX-1 anonymous units so a probe burns exactly one more."""
    for _ in range(MAX_REQUESTS_PER_WINDOW - 1):
        await client.get("/health")


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


async def test_405_route_miss_is_metered_and_shape_unchanged_within_budget():
    """F4 widened law (old pin: 405 shape only, metering unpinned -- the enumeration-oracle gap): a 405 on a
    known route keeps its exact envelope shape within budget AND meters -- over budget the refusal is the
    JSON-RPC RATE_LIMITED-shaped envelope here (REST surface).
    """
    process_state_reset()
    async with _app_client() as client:
        resp = await client.post("/health")
        assert resp.status_code == 405
        assert resp.json()["details"] == {"status_code": 405}
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.post("/health")
        refused = await client.post("/health")
        assert refused.status_code == 429
        assert refused.json()["code"] == "anonymous_throttled"


@pytest.mark.parametrize(
    "path",
    [
        "/health",
        "/ready",
        "/.well-known/crawler-service",
        "/v1/capabilities",
        "/v1/agent-hints",
        "/openapi.json",
    ],
)
async def test_every_exempt_get_is_metered(path):
    """F1: the five exempt GETs have no free surfaces -- over budget each
    refuses with the envelope, within budget each answers normally."""
    process_state_reset()
    async with _app_client() as client:
        first = await client.get(path)
        assert first.status_code == 200, path
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            await client.get(path)
        refused = await client.get(path)
        assert refused.status_code == 429, path
        assert refused.json()["code"] == "anonymous_throttled", path


async def test_healthcheck_hits_consume_the_same_ip_budget():
    """Owner ruling option (a): /health and /ready meter on the SAME per-IP anonymous budget as everything
    else -- measured cadence 2 hits/window = 1.7% of 120, no collision, no ratchet row. A healthcheck from
    a busy host IP shares that host's budget: the law, never a shadow exemption.
    """
    process_state_reset()
    async with _app_client() as client:
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            assert (await client.get("/health")).status_code == 200
        refused = await client.get("/health")
        assert refused.status_code == 429
        assert refused.json()["code"] == "anonymous_throttled"
        # The refusal is total: /ready on the same bucket refuses too.
        assert (await client.get("/ready")).status_code == 429


async def test_validation_422_consumes_exactly_one_anonymous_unit():
    """Single-count law: a 422 from a MATCHED route consumes ONE anonymous
    unit at the request stage (no response-stage double count) -- from a
    MAX-1 baseline, the next request refuses. Rejected requests are work."""
    process_state_reset()
    async with _app_client() as client:
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            resp = await client.post("/v1/crawl", json={})
            assert resp.status_code == 422, resp.status_code
        refused = await client.post("/v1/crawl", json={})
        assert refused.status_code == 429, "422 consumed MAX units; the next refuses"


async def test_405_on_a_caller_metered_route_is_anonymously_metered():
    """HIGH-1. The law: every request consumes one anonymous unit at the REQUEST stage -- a 405, caller's key
    or not, burns the anonymous IP bucket.
    """
    process_state_reset()
    async with _app_client() as client:
        for _ in range(MAX_REQUESTS_PER_WINDOW):
            resp = await client.options("/v1/crawl")
            assert resp.status_code == 405, resp.status_code
        refused = await client.options("/v1/crawl")
        assert refused.status_code == 429, "405 enumeration oracle must meter"
        assert refused.json()["code"] == "anonymous_throttled"


async def test_oversized_body_413_consumes_exactly_one_anonymous_unit():
    """HIGH-2: middleware ordering made the throttle INNERMOST, so 413/403 short-circuited before it saw the
    request -- the docstring claimed rejected requests consume budget and the code disagreed. Fix: the
    throttle is now OUTERMOST; one 413 burns one unit.
    """
    process_state_reset()
    async with _app_client() as client:
        await _fill_baseline(client)
        oversized = await client.post(
            "/v1/crawl", content=b"x" * 17 * 1024 * 1024, headers={"content-type": "application/json"}
        )
        assert oversized.status_code == 413, oversized.status_code
        await _assert_next_unit_refuses(client)


async def test_origin_rejected_403_consumes_exactly_one_anonymous_unit():
    """HIGH-2, origin arm: a hostile-Origin request is rejected 403 and still
    burns exactly one anonymous unit from the MAX-1 baseline."""
    process_state_reset()
    async with _app_client() as client:
        await _fill_baseline(client)
        rejected = await client.get("/v1/capabilities", headers={"origin": "http://evil.example"})
        assert rejected.status_code == 403, rejected.status_code
        await _assert_next_unit_refuses(client)


async def test_presented_credential_mcp_still_skips_after_reorder():
    """HIGH-2 regression walk: after the throttle moved OUTERMOST, /mcp with a
    presented credential still skips the anonymous bucket entirely (the #664
    layer meters it); MAX+5 credential requests never refuse."""
    process_state_reset()
    async with _app_client() as client:
        for _ in range(MAX_REQUESTS_PER_WINDOW + 5):
            resp = await client.post(
                "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "ping"}, headers={"x-api-key": "some-key"}
            )
            assert resp.status_code != 429, resp.status_code


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
