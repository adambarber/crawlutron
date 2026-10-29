"""JSON-RPC 2.0 helpers and MCP 2026-07-28 request-metadata validation: the
protocol is stateless, every request carrying version, identity, and capabilities
in ``_meta``; a request missing them is malformed (``-32602``/400).
"""

from __future__ import annotations

import base64
from typing import Any

from crawlutron.mcp.constants import (
    INVALID_PARAMS,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
)
from crawlutron.models.json_value import JsonValue


class ProtocolValidationError(Exception):
    """Raised when a request fails header/``_meta`` validation.

    Carries the HTTP status and the pre-built JSON-RPC error payload so the
    router can respond without re-deriving the error shape.
    """

    def __init__(self, status_code: int, payload: JsonValue) -> None:
        message: JsonValue = "validation error"
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                payload_message = error.get("message")
            # A present-but-None message must not render as str(None).
            message = (
                payload_message
                if isinstance(payload_message, str) and payload_message
                else "validation error"
            )
        else:
            message = "validation error"
        super().__init__(str(message))
        self.status_code = status_code
        self.payload = payload


def server_info() -> JsonValue:
    """The ``Implementation`` identity servers include in every result's meta."""
    return {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}


def result_response(request_id: int | str, result: JsonValue) -> JsonValue:
    """Wrap a result dict in a JSON-RPC result response with ``serverInfo`` meta."""
    if isinstance(result, dict):
        meta = result.setdefault("_meta", {})
        if isinstance(meta, dict):
            meta.setdefault(META_SERVER_INFO, server_info())
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    data: JsonValue = None,
) -> dict[str, JsonValue]:
    """Build a JSON-RPC error response (``id`` omitted for malformed requests)."""
    error: dict[str, JsonValue] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    payload: dict[str, JsonValue] = {"jsonrpc": "2.0", "error": error}
    if request_id is not None:
        payload["id"] = request_id
    return payload


def _require_meta_key(
    meta: dict[str, JsonValue], key: str, expected: type[Any], label: str, request_id: int | str
) -> object:
    """Return ``meta[key]``, or reject naming what was wrong -- absent vs
    wrong-typed are distinct corrections, so one function keeps the reasons
    agreeing. ``label`` is the spec's name for the type, shown on the wire.
    """
    value = meta.get(key)
    if not isinstance(value, expected):
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                INVALID_PARAMS,
                f"Missing required _meta key: {key}"
                if key not in meta
                else f"Invalid _meta key (must be {label}): {key}",
            ),
        )
    return value


def validate_meta(params: JsonValue | None, request_id: int | str) -> None:
    """Reject requests missing the per-request metadata this revision requires:
    raise :class:`ProtocolValidationError` (HTTP 400, ``-32602``) unless ``_meta``
    declares ``protocolVersion`` and ``clientCapabilities``.
    """
    meta = (params if isinstance(params, dict) else {}).get("_meta")
    if not isinstance(meta, dict):
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                INVALID_PARAMS,
                "Missing _meta; requests must carry "
                "io.modelcontextprotocol/protocolVersion and clientCapabilities",
            ),
        )
        # Both are checked for presence and shape. Neither is returned: the router re-reads
        # the version to compare it against the header, so a second spelling is a second copy.
    _require_meta_key(meta, META_PROTOCOL_VERSION, str, "a string", request_id)
    _require_meta_key(meta, META_CLIENT_CAPABILITIES, dict, "an object", request_id)


def decode_header_value(value: str) -> str:
    """Decode the ``=?base64?...?=`` sentinel for non-ASCII header values, which
    servers MUST decode before comparing to the body value; a sentinel-looking
    value that fails to decode is returned unchanged.
    """
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?") : -2]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return value
    return value


__all__ = [
    "ProtocolValidationError",
    "decode_header_value",
    "error_response",
    "result_response",
    "server_info",
    "validate_meta",
]
