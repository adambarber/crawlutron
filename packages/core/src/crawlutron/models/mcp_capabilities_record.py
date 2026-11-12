"""The MCP block of the capabilities payload."""

from typing import TypedDict


class McpCapabilitiesRecord(TypedDict):
    """The ``capabilities["mcp"]`` block, spelled where the endpoint writes it."""

    endpoint: str
    protocol_versions: list[str]
    tools: list[str]
