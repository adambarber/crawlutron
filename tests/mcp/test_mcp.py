"""MCP 2026-07-28 streamable-HTTP endpoint behavior.

Wire-level protocol over ASGI: metadata validation, discover, tools list/call,
Origin handling, and the JSON-RPC error surface (stateless, no initialize)."""

from __future__ import annotations

import json

import httpx
import pytest

from crawlutron.models.json_value import JsonValue
from tests._assertions import one_search_result
from tests._policy_factories import canonical

_PROTOCOL_VERSION = "2026-07-28"

_META: dict[str, JsonValue] = {
    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


def _body(
    method: str, *, params: dict[str, JsonValue] | None = None, request_id: int = 1
) -> dict[str, JsonValue]:
    # ``_META`` is copied per call so a test that mutates a request's _meta cannot poison siblings.
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {"_meta": {**_META}},
    }


def _headers(body: dict[str, JsonValue], **overrides: str | None) -> dict[str, str]:
    headers = {
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "Mcp-Method": str(body["method"]),
        "Accept": "application/json, text/event-stream",
    }
    params = body.get("params")
    if body.get("method") == "tools/call":
        assert isinstance(params, dict)
        headers["Mcp-Name"] = str(params.get("name", ""))
    for key, value in overrides.items():
        if value is None:
            headers.pop(key, None)
        else:
            headers[key] = value
    return headers


async def _mcp(client, body: dict[str, JsonValue], headers: dict[str, str | None] | None = None):
    return await client.post("/mcp", json=body, headers=_headers(body, **(headers or {})))


async def _assert_complete(
    client, body: dict[str, JsonValue], headers: dict[str, str | None] | None = None
) -> dict[str, JsonValue]:
    """Post one request, assert it succeeded, and return its result.

    Every result must carry ``resultType: "complete"``; the model drops
    undeclared keys, so this reads the real wire shape."""
    resp = await _mcp(client, body, headers)
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert isinstance(result, dict)
    assert result["resultType"] == "complete"
    return result


async def _assert_complete_tool(
    client,
    body: dict[str, JsonValue],
    headers: dict[str, str | None] | None = None,
    *,
    _check_error: bool = True,
) -> dict[str, JsonValue]:
    """Assert a completed *tool* result, and return it.

    ``isError`` is a tool result's field, not a discover result's -- folding it
    into the general helper would assert what the spec does not promise there."""
    result = await _assert_complete(client, body, headers)
    if _check_error:
        assert result["isError"] is False, result
    return result


async def _assert_header_mismatch(
    client, *, body: dict[str, JsonValue], headers: dict[str, str | None] | None = None, code: int = -32020
) -> None:
    """Assert the endpoint rejected headers that are missing or contradict the body.

    Five rejections are one spec rule from different angles; ``code`` is a
    parameter because an absent ``_meta`` is InvalidParams (-32602)."""
    resp = await _mcp(client, body, headers)
    assert resp.status_code == 400, resp.text
    assert _error_of(resp)["code"] == code, resp.text


def _text_of(item: JsonValue) -> str:
    """The text member of one content item, checked so string reads stay honest."""
    assert isinstance(item, dict)
    return str(item["text"])


def _meta_of(body: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """The mutable _meta block a request-body test edits."""
    params = body["params"]
    assert isinstance(params, dict)
    meta = params["_meta"]
    assert isinstance(meta, dict)
    return meta


def _error_of(resp) -> dict[str, JsonValue]:
    """The JSON-RPC error object of a rejected request, checked to be a dict."""
    error = resp.json()["error"]
    assert isinstance(error, dict), resp.text
    return error


def _payload_dict(resp) -> dict[str, JsonValue]:
    """The response body as a dict, checked so the next read stays honest."""
    payload = resp.json()
    assert isinstance(payload, dict), resp.text
    return payload


def _member(payload: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    """A dict-valued member of a wire payload or result, checked."""
    value = payload[key]
    assert isinstance(value, dict), (key, value)
    return value


def _items(result: dict[str, JsonValue], key: str) -> list[JsonValue]:
    """A list-valued member of a wire result, checked so iteration stays honest."""
    value = result[key]
    assert isinstance(value, list), (key, value)
    return value


def _item(result: dict[str, JsonValue], key: str, index: int = 0) -> dict[str, JsonValue]:
    """One dict item of a result's list member, checked so field reads stay honest."""
    value = _items(result, key)[index]
    assert isinstance(value, dict), (key, value)
    return value


async def _assert_invalid_request(client, *, body: dict[str, JsonValue]) -> None:
    """Assert a malformed JSON-RPC envelope is refused as Invalid Request."""
    resp = await _mcp(client, body)
    assert resp.status_code == 400, resp.text
    assert _error_of(resp)["code"] == -32600, resp.text


async def _tool_call(client, name: str, arguments: dict[str, JsonValue]) -> dict[str, JsonValue]:
    """Invoke one MCP tool; return its structuredContent on success."""
    structured = _member(
        await _assert_complete_tool(
            client, _body("tools/call", params={"name": name, "arguments": arguments, "_meta": _META})
        ),
        "structuredContent",
    )
    return structured


def _search_or_discover(name: str) -> dict[str, JsonValue]:
    """A discover body, or a search tools/call body (the Mcp-Name rules turn on it)."""
    return (
        _body("tools/call", params={"name": "search", "arguments": {"query": "x"}, "_meta": _META})
        if name == "search"
        else _body("server/discover")
    )


class TestRequestMetadataValidation:
    @pytest.mark.parametrize(("name", "header"), [("server/discover", "Mcp-Method"), ("search", "Mcp-Name")])
    async def test_missing_request_metadata_header_rejected(self, async_client, name, header):
        body = _search_or_discover(name)
        await _assert_header_mismatch(async_client, body=body, headers={header: None})

    @pytest.mark.parametrize(
        ("name", "header", "wrong"),
        [("server/discover", "Mcp-Method", "tools/list"), ("search", "Mcp-Name", "crawl")],
    )
    async def test_mismatched_request_metadata_header_rejected(self, async_client, name, header, wrong):
        body = _search_or_discover(name)
        await _assert_header_mismatch(async_client, body=body, headers={header: wrong})

    async def test_unsupported_protocol_version_reports_supported(self, async_client):
        body = _body("server/discover")
        _meta_of(body)["io.modelcontextprotocol/protocolVersion"] = "2025-06-18"
        resp = await _mcp(async_client, body, {"MCP-Protocol-Version": "2025-06-18"})
        assert resp.status_code == 400
        data = _member(_error_of(resp), "data")
        assert data["supported"] == [_PROTOCOL_VERSION]
        assert data["requested"] == "2025-06-18"

    async def test_header_body_protocol_version_mismatch_rejected(self, async_client):
        body = _body("server/discover")
        _meta_of(body)["io.modelcontextprotocol/protocolVersion"] = "2025-06-18"
        await _assert_header_mismatch(async_client, body=body)  # header stays 2026-07-28

    async def test_missing_meta_rejected(self, async_client):
        # An absent _meta block is InvalidParams, not a header mismatch: the headers agree with each other,
        # they just have nothing to agree about.
        await _assert_header_mismatch(
            async_client,
            body={"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}},
            code=-32602,
        )

    async def test_unknown_method_returns_404(self, async_client):
        resp = await _mcp(async_client, _body("resources/list"))
        assert resp.status_code == 404, resp.text
        assert _error_of(resp)["code"] == -32601

    async def test_non_loopback_origin_rejected(self, async_client):
        """A disallowed Origin on /mcp must yield the JSON-RPC error shape, not the
        REST envelope; the middleware is the single Origin gate, emitting per surface."""
        resp = await _mcp(async_client, _body("server/discover"), {"Origin": "http://evil.example"})
        assert resp.status_code == 403
        body = _payload_dict(resp)
        assert body.get("jsonrpc") == "2.0"
        assert _member(body, "error")["code"] == -32600
        assert "code" not in body and "message" not in body and "details" not in body

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
        assert _error_of(resp)["code"] == -32700


def _assert_dict_tool(tool: JsonValue) -> str:
    """One advertised tool: a dict with a description and an object schema; its name."""
    assert isinstance(tool, dict)
    assert tool["description"]
    assert _member(tool, "inputSchema")["type"] == "object"
    return str(tool["name"])


class TestDiscover:
    async def test_server_discover_advertises_supported_versions(self, async_client):
        result = await _assert_complete(async_client, _body("server/discover"))
        assert _PROTOCOL_VERSION in _items(result, "supportedVersions")
        assert "tools" in _member(result, "capabilities")
        ttl = result["ttlMs"]
        assert isinstance(ttl, int) and ttl > 0
        assert result["cacheScope"] == "public"
        assert _member(_member(result, "_meta"), "io.modelcontextprotocol/serverInfo")["name"] == "crawlutron"
        assert "instructions" in result

    async def test_tools_list_exposes_every_tool(self, async_client):
        """The advertised set is the whole surface, composite included: an agent
        reads this list and nothing else, so a tool that exists but is not
        advertised is a tool no agent finds."""
        result = await _assert_complete(async_client, _body("tools/list"))
        tools = _items(result, "tools")
        assert [_assert_dict_tool(tool) for tool in tools] == [
            "admit",
            "search",
            "crawl",
            "crawl_and_extract",
            "map",
            "render",
            "read_crawl_result",
            "read_crawl_trace",
            "create_job",
            "list_jobs",
            "get_job",
            "control_job",
            "delete_job",
        ]
        ttl = result["ttlMs"]
        assert isinstance(ttl, int) and ttl > 0
        assert result["cacheScope"] == "public"


class TestToolsCall:
    async def test_search_tool_returns_results(self, async_client, override_settings, fake_provider_http):
        one_search_result(override_settings, fake_provider_http)
        body = _body(
            "tools/call",
            params={"name": "search", "arguments": {"query": "worcester", "limit": 3}, "_meta": _META},
        )
        result = await _assert_complete_tool(async_client, body)
        first = _item(result, "content")
        assert first["type"] == "text"
        payload = json.loads(_text_of(first))
        data = payload["data"]
        assert isinstance(data, list) and isinstance(data[0], dict)
        assert data[0]["url"] == "https://example.gov/a"
        assert _member(result, "structuredContent")["provider"] == "tavily"

    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_crawl_tool_crawls_the_url(self, async_client, base_url):
        # No policy supplied: the full default strategy chain (direct HTTP, CFFI impersonation, headless +
        # headed browser) must be accepted over MCP; the engine picks the first working strategy.
        sc = await _tool_call(async_client, "crawl", {"url": f"{base_url}/"})
        assert sc["success"] is True
        first = _item(sc, "results")
        assert first["classification"] in ("success", "rendered")

    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_crawl_and_extract_returns_content_without_the_crawl_bulk(self, async_client, base_url):
        """The composite exists for the projection: many pages' extracted content,
        without the html, headers and diagnostics the full response repeats."""
        sc = await _tool_call(
            async_client, "crawl_and_extract", {"urls": [f"{base_url}/", f"{base_url}/blog"]}
        )

        assert sc["submitted_count"] == 2
        pages = _items(sc, "pages")
        page_dicts = [page for page in pages if isinstance(page, dict)]
        assert len(page_dicts) == len(pages)
        assert [page["url"] for page in page_dicts] == [
            canonical(f"{base_url}/").raw,
            canonical(f"{base_url}/blog").raw,
        ]
        assert all(page["status"] == "success" for page in page_dicts)
        assert all(page["markdown"] for page in page_dicts)
        for bulk in ("html", "headers", "diagnostics", "results"):
            assert bulk not in sc, f"the composite carried {bulk}"

    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_crawl_and_extract_reports_the_same_verdict_as_the_full_crawl(self, async_client, base_url):
        """The projection is faithful: the composite must not give a kinder verdict.

        Whatever the chain settles on -- HTTP sees the access-denied page, the
        stub pool cannot render it -- both doors report the same verdict."""
        full = await _tool_call(async_client, "crawl", {"url": f"{base_url}/blocked"})
        projected = await _tool_call(async_client, "crawl_and_extract", {"url": f"{base_url}/blocked"})

        original = _item(full, "results")
        page = _item(projected, "pages")
        assert [page["status"], page["classification"], page["markdown"]] == [
            original["status"],
            original["classification"],
            original["markdown"],
        ]
        assert page["markdown"] is None, "a blocked page produced content; the fixture changed"

    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_map_tool_returns_frontier(self, async_client, base_url):
        # Default policy: the full strategy chain is reachable over MCP.
        sc = await _tool_call(async_client, "map", {"url": f"{base_url}/"})
        assert isinstance(sc["all_urls"], list)

    async def test_crawl_tool_rejects_async_delivery(self, async_client, base_url):
        arguments: dict[str, JsonValue] = {"url": f"{base_url}/", "async_mode": True}
        resp = await _mcp(
            async_client,
            _body("tools/call", params={"name": "crawl", "arguments": arguments, "_meta": _META}),
        )
        result = _member(_payload_dict(resp), "result")
        assert result["isError"] is True
        assert "async_mode" in _text_of(_item(result, "content"))

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

        monkeypatch.setattr(f"crawlutron.mcp.handlers.{handler_name}", refuse)
        if name == "render":
            monkeypatch.setattr("crawlutron.mcp.handlers.get_browser_pool", lambda: object())
        resp, payload = await _call_tool(async_client, name, arguments)

        assert resp.status_code == 200 and "error" not in payload
        result = _member(payload, "result")
        assert result["isError"] is True
        assert "host_denied" in _text_of(_item(result, "content"))

    async def test_unknown_tool_returns_protocol_error(self, async_client):
        resp, payload = await _call_tool(async_client, "not_a_tool", {})
        error = _member(payload, "error")
        assert error["code"] == -32602 and "Unknown tool" in str(error["message"])

    async def test_non_object_arguments_rejected(self, async_client):
        resp, payload = await _call_tool(async_client, "search", "not-a-dict")
        assert _member(payload, "error")["code"] == -32602


async def _call_tool(
    async_client: httpx.AsyncClient, name: str, arguments: JsonValue
) -> tuple[httpx.Response, dict[str, JsonValue]]:
    """POST a ``tools/call`` for ``name`` and return ``(response, payload)``.

    ``_meta`` must never drop -- a request without it is rejected before the
    tool is named (three copies per test, one of them the edited copy)."""
    body = _body("tools/call", params={"name": name, "arguments": arguments, "_meta": _META})
    resp = await _mcp(async_client, body)
    return resp, _payload_dict(resp)


class TestInternalErrorContract:
    """N-4: an unexpected tool exception must stay inside the JSON-RPC shape."""

    async def test_unexpected_tool_exception_returns_internal_error(self, monkeypatch):
        import crawlutron.mcp.server as server_mod
        from crawlutron.mcp.server import handle_mcp_request

        async def boom(**kwargs):
            raise ValueError("simulated tool crash")

        monkeypatch.setattr(server_mod, "call_tool", boom)
        body = _body("tools/call", params={"name": "search", "arguments": {}, "_meta": {**_META}})
        status, payload = await handle_mcp_request(
            body,
            protocol_version_header=_PROTOCOL_VERSION,
            method_header="tools/call",
            name_header="search",
            settings=None,
            shutdown_event=None,
        )
        assert status == 500 and payload and isinstance(payload, dict)
        assert _member(payload, "error")["code"] == -32603
        assert payload["id"] == 1


class TestEnvelopeValidation:
    """#230: the JSON-RPC 2.0 envelope is validated, not just echoed."""

    @pytest.mark.parametrize(
        ("bool_id", "version"),
        [(True, "2.0"), (False, None), (False, "1.0"), (False, 2.0), (False, "3.0")],
    )
    async def test_a_non_conforming_envelope_is_refused(self, async_client, bool_id, version):
        """JSON-RPC 2.0: id is String/Number/NULL (bool is neither, only accidentally an int)
        and jsonrpc must be the literal "2.0"; absent is refused like any wrong value."""
        body = _body("server/discover")
        if bool_id:
            body["id"] = True
        elif version is None:
            del body["jsonrpc"]
        else:
            body["jsonrpc"] = version
        await _assert_invalid_request(async_client, body=body)

    async def test_correct_jsonrpc_with_int_id_still_works(self, async_client):
        # The new validation must not reject the valid envelope the helpers build.
        assert _PROTOCOL_VERSION in _items(
            await _assert_complete(async_client, _body("server/discover")), "supportedVersions"
        )
