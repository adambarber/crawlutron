"""Shared keyed tools/call helpers for the MCP tool suites."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from typing_extensions import TypedDict

from tests._async_harnesses import issue_key

if TYPE_CHECKING:
    from httpx import AsyncClient

    from crawlutron.models.json_value import JsonValue


class ToolStructured(TypedDict, total=False):
    """The structured-content record a successful tools/call returns."""

    resultType: str
    isError: bool
    content: list[dict[str, object]]
    structuredContent: object


PROTOCOL = "2026-07-28"

META: dict[str, JsonValue] = {
    "io.modelcontextprotocol/protocolVersion": PROTOCOL,
    "io.modelcontextprotocol/clientInfo": {"name": "test", "version": "1"},
    "io.modelcontextprotocol/clientCapabilities": {},
}

__all__ = [
    "META",
    "PROTOCOL",
    "KeyedToolCaller",
    "ToolStructured",
    "issue_key",
    "tool_call",
    "tool_error_text",
    "tool_structured",
]


class KeyedToolCaller:
    """One keyed caller's view of the MCP surface: structured results and tool errors."""

    client: AsyncClient
    secret: str

    async def structured(self, tool: str, arguments: dict[str, JsonValue]) -> ToolStructured:
        return await tool_structured(self.client, tool, arguments, key=self.secret)

    async def error_text(self, tool: str, arguments: dict[str, JsonValue]) -> str:
        return await tool_error_text(self.client, tool, arguments, key=self.secret)

    async def rest_json(self, method: str, path: str):
        """The REST door's JSON for one path, asserted 200 first."""
        rest = await getattr(self.client, method)(path, headers={"X-API-Key": self.secret})
        assert rest.status_code == 200, rest.text
        return rest.json()


def _mcp_headers(*, key: str, tool: str) -> dict[str, str]:
    return {
        "MCP-Protocol-Version": PROTOCOL,
        "Mcp-Method": "tools/call",
        "Mcp-Name": tool,
        "Accept": "application/json, text/event-stream",
        "X-API-Key": key,
    }


async def tool_call(client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str):
    """One keyed tools/call; returns the raw JSON-RPC result object."""
    response = await client.post(
        "/mcp",
        json=json.loads(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": tool, "arguments": arguments, "_meta": META},
                }
            )
        ),
        headers=_mcp_headers(key=key, tool=tool),
    )
    assert response.status_code == 200, response.text
    return response.json()["result"]


async def tool_structured(client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str):
    result = await tool_call(client, tool, arguments, key=key)
    assert result["isError"] is False, result
    return result["structuredContent"]


async def tool_error_text(
    client: AsyncClient, tool: str, arguments: dict[str, JsonValue], *, key: str
) -> str:
    result = await tool_call(client, tool, arguments, key=key)
    assert result["isError"] is True, result
    return str(result["content"][0]["text"])
