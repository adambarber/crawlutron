"""MCP 2026-07-28 protocol constants for the ``/mcp`` endpoint.

Values mirror the 2026-07-28 specification: the protocol is stateless, results
carry a ``resultType``, list/discover results carry ``ttlMs`` + ``cacheScope``,
and the reserved JSON-RPC error range ``-32020..-32099`` is used for
protocol-defined errors.

The mirror is of the codes this server can actually produce, not of the codes the
specification numbers. A code nothing returns is not documentation: it reads as
coverage from across the room, so a caller auditing the surface finds a value
with no path to it, and a future edit that wires the code up will not be caught
for having done so. ``MISSING_REQUIRED_CLIENT_CAPABILITY`` and
``META_CLIENT_INFO`` were both such values -- the 2026-07-28 spec requires only
``protocolVersion`` and ``clientCapabilities`` in ``_meta``, so nothing ever
raised the one or read the other. ``test_every_protocol_constant_is_produced``
holds the line.
"""

from app.vocabulary.identity import SERVICE_ID, SERVICE_VERSION

# The version this server implements, and the only one it accepts. Named apart
# from the list because the two are different claims -- what we speak, and what we
# will tolerate being sent -- and because the error path for an unsupported
# version has to quote the supported set back to the caller. Not exported: the
# supported list is what other modules consume, and a name in ``__all__`` that
# nothing imports is a claim about the surface rather than a part of it.
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
    "SUPPORTED_PROTOCOL_VERSIONS",
    "UNSUPPORTED_PROTOCOL_VERSION",
]
