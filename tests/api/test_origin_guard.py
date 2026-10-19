"""The DNS-rebinding Origin guard is not MCP-only: a hostile Origin on a REST GET
is rejected, while same-origin (loopback) and absent-Origin tooling pass.
curl, bots and agents send no Origin, so absent stays allowed (#224).
"""

from __future__ import annotations


async def test_rest_get_with_hostile_origin_is_rejected(async_client):
    """A cross-origin browser Origin on a REST GET must be refused, not served."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


async def test_rest_get_without_origin_allowed(async_client):
    """Tooling that sends no Origin (curl, bots, agents) keeps working."""
    resp = await async_client.get("/v1/capabilities")
    assert resp.status_code == 200


async def test_rest_get_with_loopback_origin_allowed(async_client):
    """A same-origin (loopback) browser request is permitted."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://127.0.0.1:8080"})
    assert resp.status_code == 200
