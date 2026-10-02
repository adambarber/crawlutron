from __future__ import annotations

from pydantic import BaseModel, Field


class McpCapabilities(BaseModel):
    """Declared MCP capability for ``/v1/capabilities``."""

    endpoint: str = Field(description="The MCP streamable-HTTP endpoint path.")
    protocol_versions: list[str] = Field(description="MCP protocol versions the server implements.")
