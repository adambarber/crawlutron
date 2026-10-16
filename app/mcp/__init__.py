"""Model Context Protocol (2026-07-28) endpoint for the crawler.

Exposes the four primitives (search, crawl, map, render) plus ``crawl_and_extract``
as MCP tools over stateless streamable-HTTP, with version/capabilities in ``_meta``.
"""

from app.mcp.tools import TOOL_SPECS, TOOLS_BY_NAME

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS"]
