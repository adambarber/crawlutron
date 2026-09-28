"""Model Context Protocol (2026-07-28) endpoint for the crawler.

Exposes the four crawler primitives (search, crawl, map, render) as MCP tools
over the stateless streamable-HTTP transport. No ``initialize`` handshake and
no sessions: every request carries its protocol version and client capabilities
in ``_meta``.
"""

from app.mcp.tools import TOOL_SPECS, TOOLS_BY_NAME

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS"]
