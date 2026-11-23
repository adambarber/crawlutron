"""JSON-RPC 2.0 error responses carry an explicit id:null (#1357)."""

from __future__ import annotations

from tests.mcp.test_mcp import _PROTOCOL_VERSION, _body, _mcp, _member, _payload_dict


async def test_parse_error_response_carries_null_id(async_client):
    """An undetectable id is reported as null, never omitted, on -32700."""
    resp = await async_client.post(
        "/mcp",
        content="{not json",
        headers={"MCP-Protocol-Version": _PROTOCOL_VERSION, "Mcp-Method": "server/discover"},
    )
    assert resp.status_code == 400
    body = _payload_dict(resp)
    assert _member(body, "error")["code"] == -32700
    assert body["id"] is None


async def test_invalid_request_response_carries_null_id(async_client):
    """A missing-version envelope answers -32600 with id:null."""
    body = _body("server/discover")
    del body["jsonrpc"]
    resp = await _mcp(async_client, body)
    assert resp.status_code == 400, resp.text
    payload = _payload_dict(resp)
    assert _member(payload, "error")["code"] == -32600
    assert payload["id"] is None
