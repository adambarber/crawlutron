"""MCP 2026-07-28 streamable-HTTP dispatch for ``POST /mcp``.

Stateless protocol: headers validated against the body, ``server/discover`` /
``tools/list`` / ``tools/call`` dispatched, unknown methods 404 with ``-32601``.
"""

from __future__ import annotations

import logging
from typing import Any, cast
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
from app.models.json_value import JsonValue

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


def discover_result() -> dict[str, JsonValue]:
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


def tools_list_result() -> dict[str, JsonValue]:
    from app.mcp.tools import TOOL_SPECS

    tools: list[dict[str, JsonValue]] = []
    for spec in TOOL_SPECS:
        definition: dict[str, JsonValue] = {
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
        "tools": cast(JsonValue, tools),
        "ttlMs": DEFAULT_TTL_MS,
        "cacheScope": CACHE_SCOPE,
    }


def _validate_transport(
    request_id: int | str,
    method: Any,
    params: dict[str, JsonValue],
    protocol_version_header: str | None,
    method_header: str | None,
    name_header: str | None,
) -> None:
    """Validate request metadata headers against the body.

    A missing/unsupported protocol-version header is reported before ``_meta``
    problems so legacy clients (no ``_meta``, no header) get an UnsupportedProtocolVersionError.
    """
    _validate_protocol_version(request_id, protocol_version_header)
    validate_meta(params, request_id)
    _validate_meta_matches_header(request_id, params, protocol_version_header)
    _validate_method_header(request_id, method, method_header)
    if method == "tools/call":
        _validate_name_header(request_id, params, name_header)


def _protocol_error(
    request_id: int | str, code: int, message: str, data: JsonValue | None = None
) -> ProtocolValidationError:
    """One constructor for the 400 envelope every header check raises."""
    return ProtocolValidationError(400, error_response(request_id, code, message, data=data))


def _validate_protocol_version(request_id: int | str, header: str | None) -> None:
    """The header must exist and name a supported protocol version."""
    if header is None:
        raise _protocol_error(request_id, HEADER_MISMATCH, "Missing required header: MCP-Protocol-Version")
    if header not in SUPPORTED_PROTOCOL_VERSIONS:
        raise _protocol_error(
            request_id,
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            data={"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": header},
        )


def _validate_meta_matches_header(
    request_id: int | str, params: dict[str, JsonValue], header: str | None
) -> None:
    """The body's _meta protocol version must agree with the header."""
    meta = params.get("_meta")
    meta_version = (meta if isinstance(meta, dict) else {}).get(META_PROTOCOL_VERSION)
    if meta_version != header:
        raise _protocol_error(
            request_id,
            HEADER_MISMATCH,
            f"MCP-Protocol-Version header '{header}' does not match _meta '{meta_version}'",
        )


def _validate_method_header(request_id: int | str, method: Any, header: str | None) -> None:
    """The Mcp-Method header must exist and name the body's method."""
    if header is None:
        raise _protocol_error(request_id, HEADER_MISMATCH, "Missing required header: Mcp-Method")
    if header != method:
        raise _protocol_error(
            request_id, HEADER_MISMATCH, f"Mcp-Method header '{header}' does not match method '{method}'"
        )


def _validate_name_header(request_id: int | str, params: dict[str, JsonValue], header: str | None) -> None:
    """On tools/call the Mcp-Name header must decode to the body's tool name."""
    tool_name = params.get("name")
    if header is None:
        raise _protocol_error(request_id, HEADER_MISMATCH, "Missing required header: Mcp-Name")
    if decode_header_value(header) != tool_name:
        raise _protocol_error(
            request_id,
            HEADER_MISMATCH,
            f"Mcp-Name header '{header}' does not match tool name '{tool_name}'",
        )


async def call_tool(
    request_id: int | str,
    body: dict[str, JsonValue],
    settings,
    shutdown_event,
    concurrency_tracker=None,
    job_store=None,
    caller=None,
) -> JsonValue:
    from app.mcp.tools import TOOLS_BY_NAME, validation_message

    params = cast(dict[str, Any], body)

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
        # that knows the tool's request model, so a tool cannot be added that surfaces a
        # pydantic error as a transport failure instead of an is_error result.
        return result_response(
            request_id,
            {
                "resultType": "complete",
                "content": [{"type": "text", "text": validation_message(error)}],
                "isError": True,
            },
        )
    result = await spec.handler(
        payload,
        settings=settings,
        shutdown_event=shutdown_event,
        concurrency_tracker=concurrency_tracker,
        job_store=job_store,
        caller=caller,
    )
    success: dict[str, Any] = {
        "resultType": "complete",
        "content": [{"type": "text", "text": result.text}],
        "isError": result.is_error,
    }
    if result.structured is not None:
        success["structuredContent"] = result.structured
    return result_response(request_id, success)


def _envelope_rejection(body: dict[str, JsonValue]) -> tuple[int, JsonValue] | None:
    """Return the Invalid Request rejection for a malformed JSON-RPC envelope.
    A wrong/absent ``jsonrpc`` version or a boolean/non-str-or-int ``id`` is
    refused here, before the request is dispatched.
    """
    if body.get("jsonrpc") != "2.0":
        return 400, error_response(None, INVALID_REQUEST, "Invalid JSON-RPC request")
    if "id" in body:
        request_id = body.get("id")
        if isinstance(request_id, bool) or not isinstance(request_id, (int, str)):
            return 400, error_response(None, INVALID_REQUEST, "Request id must be an integer or string")
    return None


async def handle_mcp_request(
    body: dict[str, JsonValue],
    *,
    protocol_version_header: str | None,
    method_header: str | None,
    name_header: str | None,
    settings,
    shutdown_event,
    concurrency_tracker=None,
    job_store=None,
    caller=None,
) -> tuple[int, JsonValue | None]:
    """Process one JSON-RPC message; returns ``(http_status, payload_or_None)``.

    A ``None`` payload means the request was accepted as a notification (202).
    """
    rejection = _envelope_rejection(body)
    if rejection is not None:
        return rejection
    if "id" not in body:
        # This revision defines no client-sent notifications over streamable
        # HTTP that the server must act on; accept and drain.
        return 202, None
    request_id = cast(int | str, body.get("id"))
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
            return 200, await call_tool(
                request_id,
                params,
                settings,
                shutdown_event,
                concurrency_tracker=concurrency_tracker,
                job_store=job_store,
                caller=caller,
            )
        return 404, error_response(request_id, METHOD_NOT_FOUND, f"Method not found: {method}")
    except ProtocolValidationError as error:
        return error.status_code, error.payload
    except Exception:
        logger.exception("mcp_request_failed", extra={"method": method, "request_id": request_id})
        return 500, error_response(request_id, INTERNAL_ERROR, "Internal error")


__all__ = ["discover_result", "handle_mcp_request", "origin_allowed", "tools_list_result"]
