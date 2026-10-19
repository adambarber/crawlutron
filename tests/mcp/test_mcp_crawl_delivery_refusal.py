"""The MCP crawl delivery-refusal is keyed on the effective VALUE, not field presence.

An explicit no-op (async_mode=false, webhook_url=null, output_format=null) is accepted;
only a meaningful value is refused, including the REST-only silent no-op output_format (#238).
"""

from __future__ import annotations

import pytest

from tests._browser_doubles import FakeBrowserPool, owned_browser_pool

_PROTOCOL_VERSION = "2026-07-28"

_META = {
    "io.modelcontextprotocol/protocolVersion": _PROTOCOL_VERSION,
    "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1.0"},
    "io.modelcontextprotocol/clientCapabilities": {},
}


@pytest.fixture()
def browser_pool_stub():
    """A real browser pool so the crawl chain runs instead of refusing with a 503."""
    with owned_browser_pool(FakeBrowserPool()):
        yield


async def _crawl_call(client, arguments: dict) -> dict:
    body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "crawl", "arguments": arguments, "_meta": _META},
    }
    headers = {
        "MCP-Protocol-Version": _PROTOCOL_VERSION,
        "Mcp-Method": "tools/call",
        "Mcp-Name": "crawl",
        "Accept": "application/json, text/event-stream",
    }
    resp = await client.post("/mcp", json=body, headers=headers)
    return resp.json()["result"]


class TestCrawlDeliveryRefusal:
    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_accepted_explicit_async_mode_false(self, async_client, base_url):
        """An explicit async_mode=false is the no-op default, not a delivery request;
        it must not be refused on field presence."""
        result = await _crawl_call(async_client, {"url": f"{base_url}/", "async_mode": False})
        assert result["isError"] is False, result

    @pytest.mark.usefixtures("browser_pool_stub")
    async def test_accepted_explicit_webhook_url_null(self, async_client, base_url):
        """An explicit webhook_url=null is the no-op default, not a webhook delivery
        request; it must not be refused on field presence."""
        result = await _crawl_call(async_client, {"url": f"{base_url}/", "webhook_url": None})
        assert result["isError"] is False, result

    async def test_refused_output_format_csv(self, async_client, base_url):
        """output_format is a silent no-op over MCP (REST-only); an explicit non-null
        value must be refused like async_mode/webhook_url, never silently dropped."""
        result = await _crawl_call(async_client, {"url": f"{base_url}/", "output_format": "csv"})
        assert result["isError"] is True
        assert "output_format" in result["content"][0]["text"]

    async def test_refused_async_mode_true(self, async_client, base_url):
        """A meaningful async_mode=true is still refused; the value-keyed check must
        not admit the value that actually requests unsupported delivery."""
        result = await _crawl_call(async_client, {"url": f"{base_url}/", "async_mode": True})
        assert result["isError"] is True
        assert "async_mode" in result["content"][0]["text"]
