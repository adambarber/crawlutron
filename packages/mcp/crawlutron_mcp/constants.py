"""MCP 2026-07-28 protocol constants for the ``/mcp`` endpoint.

Values mirror the spec; only codes this server can produce are declared --
``test_every_protocol_constant_is_produced`` holds the line.
"""

from crawlutron.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

MCP_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = [MCP_PROTOCOL_VERSION]
MCP_SERVER_NAME = SERVICE_ID
MCP_SERVER_VERSION = SERVICE_VERSION

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specified error codes (reserved sub-range -32020..-32099).
HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
INVALID_API_KEY = -32001
RATE_LIMITED = -32029

# Per-request / per-result metadata keys (see ``_meta`` in the spec).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Cache metadata required on list/discover results (2026-07-28).
DEFAULT_TTL_MS = 300_000
DISCOVER_TTL_MS = 3_600_000
CACHE_SCOPE = "public"

__all__ = [
    "CACHE_SCOPE",
    "DEFAULT_TTL_MS",
    "DISCOVER_TTL_MS",
    "HEADER_MISMATCH",
    "INVALID_API_KEY",
    "INTERNAL_ERROR",
    "INVALID_PARAMS",
    "INVALID_REQUEST",
    "MCP_SERVER_NAME",
    "MCP_SERVER_VERSION",
    "META_CLIENT_CAPABILITIES",
    "META_PROTOCOL_VERSION",
    "META_SERVER_INFO",
    "METHOD_NOT_FOUND",
    "PARSE_ERROR",
    "RATE_LIMITED",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "UNSUPPORTED_PROTOCOL_VERSION",
]
