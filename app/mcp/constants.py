"""MCP 2026-07-28 protocol constants for the ``/mcp`` endpoint.

Values mirror the 2026-07-28 specification: the protocol is stateless, results
carry a ``resultType``, list/discover results carry ``ttlMs`` + ``cacheScope``,
and the reserved JSON-RPC error range ``-32020..-32099`` is used for
protocol-defined errors.
"""

from app.constants import SERVICE_ID, SERVICE_VERSION

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
MISSING_REQUIRED_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Per-request / per-result metadata keys (see ``_meta`` in the spec).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# Cache metadata required on list/discover results (2026-07-28).
DEFAULT_TTL_MS = 300_000
DISCOVER_TTL_MS = 3_600_000
CACHE_SCOPE = "public"

__all__ = [
    "MCP_PROTOCOL_VERSION",
    "SUPPORTED_PROTOCOL_VERSIONS",
    "MCP_SERVER_NAME",
    "MCP_SERVER_VERSION",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "HEADER_MISMATCH",
    "MISSING_REQUIRED_CLIENT_CAPABILITY",
    "UNSUPPORTED_PROTOCOL_VERSION",
    "META_PROTOCOL_VERSION",
    "META_CLIENT_INFO",
    "META_CLIENT_CAPABILITIES",
    "META_SERVER_INFO",
    "DEFAULT_TTL_MS",
    "DISCOVER_TTL_MS",
    "CACHE_SCOPE",
]
