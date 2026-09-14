"""MCP 2026-07-28 streamable-HTTP endpoint behavior.

Tests the protocol contract at the wire level over the ASGI transport: request
metadata validation (headers + ``_meta``), ``server/discover``, ``tools/list``,
``tools/call`` dispatch, Origin handling, and the JSON-RPC error surface.
Written against the 2026-07-28 specification (stateless, per-request metadata,
no ``initialize`` handshake).
"""

from __future__ import annotations

import json

import pytest
from _testkit import provider_results

_PROTOCOL_VERSION = "2026-07-28"

_META = {
    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _body(method: str, *, params: dict | None = None, request_id: int = 1) -> dict:
    # ``_META`` is copied per call so a test that mutates a request's _meta
    # (e.g. to probe a mismatched protocol version) cannot poison sibling tests
    # through a shared mutable module-level dict.
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {"_meta": {**_META}},
    }


def _headers(body: dict, **overrides) -> dict:
    headers = {
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "Mcp-Method": body["method"],
        "Accept": "application/json, text/event-stream",
    }
    if body.get("method") == "tools/call":
        headers["Mcp-Name"] = (body.get("params") or {}).get("name", "")
    for key, value in overrides.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers


async def _mcp(client, body: dict, headers: dict | None = None):
    return await client.post("/mcp", json=body, headers=_headers(body, **(headers or {})))


async def _tool_call(client, name: str, arguments: dict) -> dict:
    """Invoke one MCP tool; return its structuredContent on success."""
    resp = await _mcp(
        client,
        _body("tools/call", params={"name": name, "arguments": arguments, "_meta": _META}),
    )
    assert resp.status_code == 200
    result = resp.json()["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    return result["structuredContent"]


class TestRequestMetadataValidation:
    async def test_missing_protocol_version_header_rejected(self, async_client):
        resp = await _mcp(async_client, _body("server/discover"), {"MCP-Protocol-Version": None})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_unsupported_protocol_version_reports_supported(self, async_client):
        body = _body("server/discover")
        body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2025-06-18"
        resp = await _mcp(async_client, body, {"MCP-Protocol-Version": "2025-06-18"})
        assert resp.status_code == 400
        error = resp.json()["error"]
        assert error["code"] == -32022
        assert error["data"]["supported"] == [_PROTOCOL_VERSION]
        assert error["data"]["requested"] == "2025-06-18"

    async def test_header_body_protocol_version_mismatch_rejected(self, async_client):
        body = _body("server/discover")
        body["params"]["_meta"]["io.modelcontextprotocol/protocolVersion"] = "2025-06-18"
        resp = await _mcp(async_client, body)  # header stays 2026-07-28
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_missing_meta_rejected(self, async_client):
        resp = await _mcp(
            async_client, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}}
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32602

    async def test_missing_mcp_method_header_rejected(self, async_client):
        resp = await _mcp(async_client, _body("server/discover"), {"Mcp-Method": None})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_mcp_method_header_body_mismatch_rejected(self, async_client):
        resp = await _mcp(async_client, _body("server/discover"), {"Mcp-Method": "tools/list"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_missing_mcp_name_header_on_tools_call_rejected(self, async_client):
        body = _body("tools/call", params={"name": "search", "arguments": {"query": "x"}, "_meta": _META})
        resp = await _mcp(async_client, body, {"Mcp-Name": None})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_mcp_name_header_body_mismatch_rejected(self, async_client):
        body = _body("tools/call", params={"name": "search", "arguments": {"query": "x"}, "_meta": _META})
        resp = await _mcp(async_client, body, {"Mcp-Name": "crawl"})
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32020

    async def test_unknown_method_returns_404(self, async_client):
        resp = await _mcp(async_client, _body("resources/list"))
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == -32601

    async def test_non_loopback_origin_rejected(self, async_client):
        resp = await _mcp(async_client, _body("server/discover"), {"Origin": "http://evil.example"})
        assert resp.status_code == 403

    async def test_notification_accepted_without_response(self, async_client):
        notification = {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {}}
        resp = await async_client.post(
            "/mcp", json=notification, headers=_headers({"method": "notifications/cancelled"})
        )
        assert resp.status_code == 202
        assert resp.content == b""

    async def test_malformed_json_returns_parse_error(self, async_client):
        resp = await async_client.post(
            "/mcp",
            content="{not json",
            headers={"MCP-Protocol-Version": _PROTOCOL_VERSION, "Mcp-Method": "server/discover"},
        )
        assert resp.status_code == 400
        assert resp.json()["error"]["code"] == -32700


class TestDiscover:
    async def test_server_discover_advertises_supported_versions(self, async_client):
        resp = await _mcp(async_client, _body("server/discover"))
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["resultType"] == "complete"
        assert _PROTOCOL_VERSION in result["supportedVersions"]
        assert "tools" in result["capabilities"]
        assert result["ttlMs"] > 0
        assert result["cacheScope"] == "public"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "browser-crawler"
        assert "instructions" in result


class TestToolsList:
    async def test_tools_list_exposes_the_four_primitives(self, async_client):
        resp = await _mcp(async_client, _body("tools/list"))
        assert resp.status_code == 200, resp.text
        result = resp.json()["result"]
        assert result["resultType"] == "complete"
        names = [tool["name"] for tool in result["tools"]]
        assert names == ["search", "crawl", "map", "render"]
        for tool in result["tools"]:
            assert tool["description"]
            assert tool["inputSchema"]["type"] == "object"
        assert result["ttlMs"] > 0
        assert result["cacheScope"] == "public"


@pytest.fixture
def browser_pool_stub():
    """Make the browser pool present without launching a real browser.

    The MCP crawl/map tools are invoked with the full default strategy chain
    (including browser strategies) to prove every strategy is reachable over
    MCP. In the hermetic suite the direct-HTTP strategy succeeds first, so the
    stub pool is never actually used; it only needs to exist so the engine's
    ``browser_pool_unavailable`` guard does not 503.
    """
    from app.crawl.strategies.browser_pool import set_browser_pool

    class _StubPool:
        async def acquire(self):
            raise AssertionError("browser strategy should not run in a hermetic MCP test")

        def release(self, renderer):
            pass

    previous = set_browser_pool(_StubPool())
    yield
    set_browser_pool(previous)


class TestToolsCall:
    async def test_search_tool_returns_results(self, async_client, override_settings, fake_provider_http):
        override_settings(search_provider="tavily", tavily_api_key="test-key")
        fake_provider_http(
            200,
            provider_results({"title": "A", "url": "https://example.gov/a", "content": "desc"}),
        )
        body = _body(
            "tools/call",
            params={"name": "search", "arguments": {"query": "worcester", "limit": 3}, "_meta": _META},
        )
        resp = await _mcp(async_client, body)
        assert resp.status_code == 200
        result = resp.json()["result"]
        assert result["resultType"] == "complete"
        assert result["isError"] is False
        assert result["content"][0]["type"] == "text"
        payload = json.loads(result["content"][0]["text"])
        assert payload["data"][0]["url"] == "https://example.gov/a"
        assert result["structuredContent"]["provider"] == "tavily"

    async def test_crawl_tool_crawls_the_url(self, async_client, base_url, browser_pool_stub):
        # No policy supplied: the full default strategy chain (direct HTTP,
        # CFFI impersonation, headless + headed browser) must be accepted over
        # MCP; the engine picks the first working strategy.
        sc = await _tool_call(async_client, "crawl", {"url": f"{base_url}/"})
        assert sc["success"] is True
        assert sc["results"][0]["classification"] in ("success", "rendered")

    async def test_map_tool_returns_frontier(self, async_client, base_url, browser_pool_stub):
        # Default policy: the full strategy chain is reachable over MCP.
        sc = await _tool_call(async_client, "map", {"url": f"{base_url}/"})
        assert isinstance(sc["all_urls"], list)

    async def test_crawl_tool_rejects_async_delivery(self, async_client, base_url):
        body = _body(
            "tools/call",
            params={
                "name": "crawl",
                "arguments": {"url": f"{base_url}/", "async_mode": True},
                "_meta": _META,
            },
        )
        resp = await _mcp(async_client, body)
        result = resp.json()["result"]
        assert result["isError"] is True
        assert "async_mode" in result["content"][0]["text"]

    async def test_unknown_tool_returns_protocol_error(self, async_client):
        body = _body(
            "tools/call",
            params={"name": "not_a_tool", "arguments": {}, "_meta": _META},
        )
        resp = await _mcp(async_client, body)
        assert resp.status_code == 200
        error = resp.json()["error"]
        assert error["code"] == -32602
        assert "Unknown tool" in error["message"]

    async def test_non_object_arguments_rejected(self, async_client):
        body = _body(
            "tools/call",
            params={"name": "search", "arguments": "not-a-dict", "_meta": _META},
        )
        resp = await _mcp(async_client, body)
        assert resp.status_code == 200
        assert resp.json()["error"]["code"] == -32602


class TestInternalErrorContract:
    """N-4: an unexpected tool exception must stay inside the JSON-RPC shape."""

    async def test_unexpected_tool_exception_returns_internal_error(self, monkeypatch):
        import app.mcp.server as server_mod
        from app.mcp.server import handle_mcp_request

        async def boom(**kwargs):
            raise ValueError("simulated tool crash")

        monkeypatch.setattr(server_mod, "_call_tool", boom)
        body = _body("tools/call", params={"name": "search", "arguments": {}, "_meta": {**_META}})
        status, payload = await handle_mcp_request(
            body,
            protocol_version_header=_PROTOCOL_VERSION,
            method_header="tools/call",
            name_header="search",
            settings=None,
            shutdown_event=None,
        )
        assert status == 500
        assert payload["error"]["code"] == -32603
        assert payload["id"] == 1
