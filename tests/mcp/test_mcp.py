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

from tests._assertions import one_search_result
from tests._browser_doubles import FakeBrowserPool

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


async def _assert_complete(client, body: dict, headers: dict | None = None) -> dict:
    """Post one request, assert it succeeded, and return its result.

    The 2026-07-28 spec requires every result to carry ``resultType:
    "complete"``, so that is one assertion rather than a shape each test
    re-derived. The envelope is a response_model (a new top-level key is
    silently dropped by a model that does not declare it), so this reads the
    real wire shape.
    """
    resp = await _mcp(client, body, headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["resultType"] == "complete"
    return result


async def _assert_complete_tool(client, body: dict, headers: dict | None = None) -> dict:
    """Assert a completed *tool* result, and return it.

    ``isError`` is a tool result's field, not a discover result's -- a discover
    payload has no such key, so folding the check into the general helper made
    it assert something the spec does not promise there.
    """
    result = await _assert_complete(client, body, headers)
    assert result["isError"] is False, result
    return result


async def _assert_header_mismatch(
    client, *, body: dict, headers: dict | None = None, code: int = -32020
) -> None:
    """Assert the endpoint rejected headers that are missing or contradict the body.

    Five of the protocol's rejections are this single spec rule reached from
    different angles: a required header absent, a protocol-version header that
    disagrees with ``_meta``, an ``Mcp-Method``/``Mcp-Name`` header that names
    a different call than the body does. Each of the five was asserting the 400
    and the code by hand, so a change to the code would have left four of them
    still passing against the old number while only the one whose literal had
    been updated caught it.

    ``code`` is a parameter because one of the five is a different error:
    an *absent* ``_meta`` block is InvalidParams (-32602), not a header
    mismatch, and conflating the two is the mistake this helper exists to stop.
    """
    resp = await _mcp(client, body, headers)
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == code, resp.text


async def _tool_call(client, name: str, arguments: dict) -> dict:
    """Invoke one MCP tool; return its structuredContent on success."""
    return (
        await _assert_complete_tool(
            client,
            _body("tools/call", params={"name": name, "arguments": arguments, "_meta": _META}),
        )
    )["structuredContent"]


def _search_tool_body(query: str = "x") -> dict:
    """A tools/call body naming the search tool; the Mcp-Name rules turn on the name."""
    return _body("tools/call", params={"name": "search", "arguments": {"query": query}, "_meta": _META})


class TestRequestMetadataValidation:
    async def test_missing_protocol_version_header_rejected(self, async_client):
        await _assert_header_mismatch(
            async_client, body=_body("server/discover"), headers={"MCP-Protocol-Version": None}
        )

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
        await _assert_header_mismatch(async_client, body=body)  # header stays 2026-07-28

    async def test_missing_meta_rejected(self, async_client):
        # An absent _meta block is InvalidParams, not a header mismatch: the
        # headers agree with each other, they just have nothing to agree about.
        await _assert_header_mismatch(
            async_client,
            body={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            code=-32602,
        )

    @pytest.mark.parametrize(
        ("key", "label", "wrong_type"),
        [
            ("io.modelcontextprotocol/protocolVersion", "a string", 7),
            ("io.modelcontextprotocol/clientCapabilities", "an object", "nope"),
        ],
    )
    async def test_meta_key_rejection_separates_absent_from_wrong_type(
        self, async_client, key, label, wrong_type
    ):
        # Telling a client that forgot the key from one that sent the wrong
        # shape is the whole point of the reason string, and it was asserted by
        # no test and spelled out once per key in the validator -- so one of the
        # two arms could have stopped distinguishing them, or stopped agreeing
        # with the other, with the suite green.
        cases = (
            ("delete", f"Missing required _meta key: {key}"),
            (wrong_type, f"Invalid _meta key (must be {label}): {key}"),
        )
        for value, reason in cases:
            body = _body("server/discover")
            if value == "delete":
                del body["params"]["_meta"][key]
            else:
                body["params"]["_meta"][key] = value
            resp = await _mcp(async_client, body)
            assert resp.status_code == 400, resp.text
            error = resp.json()["error"]
            assert error["code"] == -32602, resp.text
            assert error["message"] == reason, resp.text

    async def test_missing_mcp_method_header_rejected(self, async_client):
        await _assert_header_mismatch(
            async_client, body=_body("server/discover"), headers={"Mcp-Method": None}
        )

    async def test_mcp_method_header_body_mismatch_rejected(self, async_client):
        await _assert_header_mismatch(
            async_client, body=_body("server/discover"), headers={"Mcp-Method": "tools/list"}
        )

    async def test_missing_mcp_name_header_on_tools_call_rejected(self, async_client):
        await _assert_header_mismatch(async_client, body=_search_tool_body(), headers={"Mcp-Name": None})

    async def test_mcp_name_header_body_mismatch_rejected(self, async_client):
        await _assert_header_mismatch(async_client, body=_search_tool_body(), headers={"Mcp-Name": "crawl"})

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
        result = await _assert_complete(async_client, _body("server/discover"))
        assert _PROTOCOL_VERSION in result["supportedVersions"]
        assert "tools" in result["capabilities"]
        assert result["ttlMs"] > 0
        assert result["cacheScope"] == "public"
        assert result["_meta"]["io.modelcontextprotocol/serverInfo"]["name"] == "browser-crawler"
        assert "instructions" in result


class TestToolsList:
    async def test_tools_list_exposes_the_four_primitives(self, async_client):
        result = await _assert_complete(async_client, _body("tools/list"))
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

    previous = set_browser_pool(FakeBrowserPool())
    yield
    set_browser_pool(previous)


class TestToolsCall:
    async def test_search_tool_returns_results(self, async_client, override_settings, fake_provider_http):
        one_search_result(override_settings, fake_provider_http)
        result = await _assert_complete_tool(
            async_client,
            _body(
                "tools/call",
                params={"name": "search", "arguments": {"query": "worcester", "limit": 3}, "_meta": _META},
            ),
        )
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

    @pytest.mark.parametrize(
        ("name", "handler_name", "arguments"),
        [
            ("crawl", "execute_crawl", {"url": "https://blocked.test/"}),
            ("map", "map_site", {"url": "https://blocked.test/"}),
            ("render", "render_endpoint_handler", {"url": "https://blocked.test/"}),
            ("search", "search_endpoint_handler", {"query": "blocked"}),
        ],
    )
    async def test_policy_refusal_is_a_tool_error_for_every_tool(
        self, async_client, monkeypatch, name, handler_name, arguments
    ):
        async def refuse(*args, **kwargs):
            raise ValueError("host_denied")

        monkeypatch.setattr(f"app.mcp.tools.{handler_name}", refuse)
        if name == "render":
            monkeypatch.setattr("app.mcp.tools.get_browser_pool", lambda: object())
        resp, payload = await _call_tool(async_client, name, arguments)

        assert resp.status_code == 200
        assert "error" not in payload
        result = payload["result"]
        assert result["isError"] is True
        assert "host_denied" in result["content"][0]["text"]

    async def test_unknown_tool_returns_protocol_error(self, async_client):
        resp, payload = await _call_tool(async_client, "not_a_tool", {})
        assert resp.status_code == 200
        error = payload["error"]
        assert error["code"] == -32602
        assert "Unknown tool" in error["message"]

    async def test_non_object_arguments_rejected(self, async_client):
        resp, payload = await _call_tool(async_client, "search", "not-a-dict")
        assert resp.status_code == 200
        assert payload["error"]["code"] == -32602


async def _call_tool(async_client, name, arguments):
    """POST a ``tools/call`` for ``name`` and return ``(response, payload)``.

    Every wire-level test here states the same request -- method, the tool name,
    the arguments, and the required ``_meta`` -- and the ``_meta`` is the part
    that must never be dropped, since a request without it is rejected before
    the tool is even named. Written per test it was three copies of the frame
    and one of them was the copy someone edited.
    """
    body = _body("tools/call", params={"name": name, "arguments": arguments, "_meta": _META})
    resp = await _mcp(async_client, body)
    return resp, resp.json()


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
