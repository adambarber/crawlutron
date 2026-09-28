"""MCP 2026-07-28 streamable-HTTP dispatch for ``POST /mcp``.

Implements the stateless protocol: every request carries its protocol version
and capabilities in ``_meta``; there is no ``initialize`` handshake and no
session. Server behaviour per the 2026-07-28 specification:

* every POST is validated against the ``MCP-Protocol-Version``, ``Mcp-Method``,
  and (for ``tools/call``) ``Mcp-Name`` headers, which MUST match the body;
* ``server/discover``, ``tools/list``, and ``tools/call`` are dispatched;
* unknown RPC methods return ``404`` with ``-32601`` (which distinguishes a
  modern server from a legacy HTTP+SSE host);
* notifications (no ``id``) are accepted with ``202`` and no body.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from pydantic import ValidationError

from app.mcp.constants import (
    CACHE_SCOPE,
    DEFAULT_TTL_MS,
    DISCOVER_TTL_MS,
    HEADER_MISMATCH,
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    META_PROTOCOL_VERSION,
    METHOD_NOT_FOUND,
    SUPPORTED_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
)
from app.mcp.protocol import (
    ProtocolValidationError,
    decode_header_value,
    error_response,
    result_response,
    validate_meta,
)

logger = logging.getLogger(__name__)

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def origin_allowed(origin: str | None) -> bool:
    """Reject cross-origin browser requests (DNS-rebinding defense).

    The service binds to loopback only and carries no caller authentication, so
    any browser origin that is not loopback is rejected with 403.
    """
    if not origin:
        return True
    try:
        parts = urlsplit(origin)
    except ValueError:
        return False
    if parts.scheme not in ("http", "https"):
        return False
    return (parts.hostname or "").lower() in _LOOPBACK_HOSTS


def discover_result() -> dict[str, Any]:
    return {
        "resultType": "complete",
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {}},
        "instructions": (
            "Policy-driven web crawler. Tools: search (discover public URLs from a query; results "
            "are not crawled), crawl (fetch one or more URLs per the /v1/crawl policy), map (URL "
            "frontier for a root), render (browser render). Search result titles and snippets are "
            "provider-supplied and unverified until crawled. The service classifies blocked/challenge "
            "outcomes but does not bypass CAPTCHAs, WAFs, login, or paywalls."
        ),
        "ttlMs": DISCOVER_TTL_MS,
        "cacheScope": CACHE_SCOPE,
    }


def tools_list_result() -> dict[str, Any]:
    from app.mcp.tools import TOOL_SPECS

    tools = []
    for spec in TOOL_SPECS:
        definition = {
            "name": spec.name,
            "title": spec.title,
            "description": spec.description,
            "inputSchema": spec.input_schema,
        }
        if spec.output_schema is not None:
            definition["outputSchema"] = spec.output_schema
        tools.append(definition)
    return {
        "resultType": "complete",
        "tools": tools,
        "ttlMs": DEFAULT_TTL_MS,
        "cacheScope": CACHE_SCOPE,
    }


def _validate_transport(
    request_id: int | str,
    method: Any,
    params: dict[str, Any],
    protocol_version_header: str | None,
    method_header: str | None,
    name_header: str | None,
) -> None:
    """Validate request metadata headers against the body.

    Order matters: a missing/unsupported protocol-version header is reported
    before ``_meta`` problems so legacy clients (which send no ``_meta`` and no
    header) receive an UnsupportedProtocolVersionError naming what we support.
    """
    if protocol_version_header is None:
        raise ProtocolValidationError(
            400,
            error_response(request_id, HEADER_MISMATCH, "Missing required header: MCP-Protocol-Version"),
        )
    if protocol_version_header not in SUPPORTED_PROTOCOL_VERSIONS:
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                data={
                    "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                    "requested": protocol_version_header,
                },
            ),
        )
    validate_meta(params, request_id)
    meta_version = (params.get("_meta") or {}).get(META_PROTOCOL_VERSION)
    if meta_version != protocol_version_header:
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                HEADER_MISMATCH,
                f"MCP-Protocol-Version header '{protocol_version_header}' does not match "
                f"_meta '{meta_version}'",
            ),
        )
    if method_header is None:
        raise ProtocolValidationError(
            400,
            error_response(request_id, HEADER_MISMATCH, "Missing required header: Mcp-Method"),
        )
    if method_header != method:
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                HEADER_MISMATCH,
                f"Mcp-Method header '{method_header}' does not match method '{method}'",
            ),
        )
    if method == "tools/call":
        tool_name = params.get("name")
        if name_header is None:
            raise ProtocolValidationError(
                400,
                error_response(request_id, HEADER_MISMATCH, "Missing required header: Mcp-Name"),
            )
        if decode_header_value(name_header) != tool_name:
            raise ProtocolValidationError(
                400,
                error_response(
                    request_id,
                    HEADER_MISMATCH,
                    f"Mcp-Name header '{name_header}' does not match tool name '{tool_name}'",
                ),
            )


async def _call_tool(
    request_id: int | str,
    params: dict[str, Any],
    settings,
    shutdown_event,
) -> dict[str, Any]:
    from app.mcp.tools import TOOLS_BY_NAME, validation_message

    name = params.get("name")
    if not isinstance(name, str):
        return error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
    spec = TOOLS_BY_NAME.get(name)
    if spec is None:
        return error_response(request_id, INVALID_PARAMS, f"Unknown tool: {name}")
    arguments = params.get("arguments")
    if not isinstance(arguments, dict):
        return error_response(request_id, INVALID_PARAMS, "Tool arguments must be an object")
    try:
        payload = spec.request_model(**arguments)
    except (ValidationError, ValueError) as error:
        # Every tool rejects a malformed call the same way, from the one place
        # that knows the tool's request model, so a tool cannot be added that
        # surfaces a pydantic error as a transport failure instead of an
        # is_error result.
        return result_response(
            request_id,
            {
                "resultType": "complete",
                "content": [{"type": "text", "text": validation_message(error)}],
                "isError": True,
            },
        )
    result = await spec.handler(payload, settings=settings, shutdown_event=shutdown_event)
    success: dict[str, Any] = {
        "resultType": "complete",
        "content": [{"type": "text", "text": result.text}],
        "isError": result.is_error,
    }
    if result.structured is not None:
        success["structuredContent"] = result.structured
    return result_response(request_id, success)


async def handle_mcp_request(
    body: dict[str, Any],
    *,
    protocol_version_header: str | None,
    method_header: str | None,
    name_header: str | None,
    settings,
    shutdown_event,
) -> tuple[int, dict[str, Any] | None]:
    """Process one JSON-RPC message; returns ``(http_status, payload_or_None)``.

    A ``None`` payload means the request was accepted as a notification (202).
    """
    if "id" not in body:
        # This revision defines no client-sent notifications over streamable
        # HTTP that the server must act on; accept and drain.
        return 202, None

    # JSON-RPC requires a request id to be an integer or string (never null);
    # reject any other shape before it reaches the response builders, which echo
    # the id verbatim.
    request_id = body.get("id")
    if not isinstance(request_id, (int, str)):
        return 400, error_response(None, INVALID_REQUEST, "Request id must be an integer or string")
    method = body.get("method")
    params = body.get("params")
    if not isinstance(params, dict):
        params = {}

    try:
        _validate_transport(
            request_id,
            method,
            params,
            protocol_version_header,
            method_header,
            name_header,
        )
        if method == "server/discover":
            return 200, result_response(request_id, discover_result())
        if method == "tools/list":
            return 200, result_response(request_id, tools_list_result())
        if method == "tools/call":
            return 200, await _call_tool(request_id, params, settings, shutdown_event)
        return 404, error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except ProtocolValidationError as error:
        return error.status_code, error.payload
    except Exception:
        logger.exception("mcp_request_failed", extra={"method": method, "request_id": request_id})
        return 500, error_response(request_id, INTERNAL_ERROR, "Internal error")


__all__ = ["discover_result", "handle_mcp_request", "origin_allowed", "tools_list_result"]
