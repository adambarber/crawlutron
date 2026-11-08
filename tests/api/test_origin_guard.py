"""The DNS-rebinding Origin guard is not MCP-only: a hostile Origin on a REST GET
is rejected, while same-origin (loopback) and absent-Origin tooling pass.
curl, bots and agents send no Origin, so absent stays allowed (#224).
"""

from __future__ import annotations


async def test_rest_get_with_hostile_origin_is_rejected(async_client):
    """A cross-origin browser Origin on a REST GET must be refused, not served."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403


async def test_rest_get_hostile_origin_returns_the_envelope(async_client):
    """The REST surface keeps the envelope body; only /mcp answers with JSON-RPC (#326)."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://evil.example"})
    assert resp.status_code == 403
    body = resp.json()
    assert set(body) == {"code", "message", "details"}
    assert body["code"] == "origin_not_allowed"
    assert "jsonrpc" not in body


async def test_rest_get_without_origin_allowed(async_client):
    """Tooling that sends no Origin (curl, bots, agents) keeps working."""
    resp = await async_client.get("/v1/capabilities")
    assert resp.status_code == 200


async def test_rest_get_with_loopback_origin_allowed(async_client):
    """A same-origin (loopback) browser request is permitted."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://127.0.0.1:8080"})
    assert resp.status_code == 200


async def test_hostile_origin_403_does_not_echo_the_origin_header(async_client):
    """#655: the details bag must not launder attacker-controlled header text
    back out -- the response names the code, never the caller's Origin value."""
    resp = await async_client.get("/v1/capabilities", headers={"Origin": "http://evil.example/<script>"})
    assert resp.status_code == 403
    body = resp.json()
    assert "http://evil.example/<script>" not in str(body)
    assert body["details"] == {}
