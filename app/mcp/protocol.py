"""JSON-RPC 2.0 helpers and MCP 2026-07-28 request-metadata validation.

The 2026-07-28 protocol is stateless: every request carries its protocol
version, client identity, and client capabilities in ``_meta``, and a request
missing the required fields is malformed (``-32602``, HTTP 400). Results must
carry ``resultType`` and SHOULD carry ``_meta.serverInfo``.
"""

from __future__ import annotations

import base64
from typing import Any

from app.mcp.constants import (
    INVALID_PARAMS,
    MCP_SERVER_NAME,
    MCP_SERVER_VERSION,
    META_CLIENT_CAPABILITIES,
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
)


class ProtocolValidationError(Exception):
    """Raised when a request fails header/``_meta`` validation.

    Carries the HTTP status and the pre-built JSON-RPC error payload so the
    router can respond without re-deriving the error shape.
    """

    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        super().__init__(str((payload.get("error") or {}).get("message", "validation error")))
        self.status_code = status_code
        self.payload = payload


def server_info() -> dict[str, str]:
    """The ``Implementation`` identity servers include in every result's meta."""
    return {"name": MCP_SERVER_NAME, "version": MCP_SERVER_VERSION}


def result_response(request_id: int | str, result: dict[str, Any]) -> dict[str, Any]:
    """Wrap a result dict in a JSON-RPC result response with ``serverInfo`` meta."""
    result.setdefault("_meta", {})
    result["_meta"].setdefault(META_SERVER_INFO, server_info())
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(
    request_id: int | str | None,
    code: int,
    message: str,
    data: Any = None,
) -> dict[str, Any]:
    """Build a JSON-RPC error response (``id`` omitted for malformed requests)."""
    payload: dict[str, Any] = {
        "jsonrpc": "2.0",
        "error": {"code": code, "message": message},
    }
    if request_id is not None:
        payload["id"] = request_id
    if data is not None:
        payload["error"]["data"] = data
    return payload


def validate_meta(params: dict[str, Any] | None, request_id: int | str) -> None:
    """Reject requests missing the per-request metadata this revision requires.

    Raises :class:`ProtocolValidationError` (HTTP 400, ``-32602``) when the
    request body does not declare ``protocolVersion`` and ``clientCapabilities``
    in ``_meta``.
    """
    meta = (params or {}).get("_meta")
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
    version = meta.get(META_PROTOCOL_VERSION)
    if not isinstance(version, str):
        # Missing and wrong-type are different caller errors (N-6): say which.
        reason = (
            f"Missing required _meta key: {META_PROTOCOL_VERSION}"
            if META_PROTOCOL_VERSION not in meta
            else f"Invalid _meta key (must be a string): {META_PROTOCOL_VERSION}"
        )
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                INVALID_PARAMS,
                reason,
            ),
        )
    capabilities = meta.get(META_CLIENT_CAPABILITIES)
    if not isinstance(capabilities, dict):
        reason = (
            f"Missing required _meta key: {META_CLIENT_CAPABILITIES}"
            if META_CLIENT_CAPABILITIES not in meta
            else f"Invalid _meta key (must be an object): {META_CLIENT_CAPABILITIES}"
        )
        raise ProtocolValidationError(
            400,
            error_response(
                request_id,
                INVALID_PARAMS,
                reason,
            ),
        )


def decode_header_value(value: str) -> str:
    """Decode the ``=?base64?...?=`` sentinel format for non-ASCII header values.

    Header values that cannot be represented as plain ASCII (or that literally
    match the sentinel pattern) are Base64-encoded; servers MUST decode before
    comparing to the body value.
    """
    if value.startswith("=?base64?") and value.endswith("?="):
        try:
            return base64.b64decode(value[len("=?base64?") : -2]).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            return value
    return value


__all__ = [
    "ProtocolValidationError",
    "server_info",
    "result_response",
    "error_response",
    "validate_meta",
    "decode_header_value",
]
