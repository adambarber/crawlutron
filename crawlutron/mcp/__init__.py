"""Model Context Protocol (2026-07-28) endpoint for the crawler.

Exposes every parity tool (see ``registry.TOOL_SPECS``) over stateless
streamable-HTTP, with version/capabilities in ``_meta``.
"""

from crawlutron.mcp.registry import TOOL_SPECS, TOOLS_BY_NAME

__all__ = ["TOOLS_BY_NAME", "TOOL_SPECS"]
