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


def _require_meta_key(
    meta: dict[str, Any], key: str, expected: type[Any], label: str, request_id: int | str
) -> Any:
    """Return ``meta[key]``, or reject with the reason naming what was wrong.

    Absent and wrong-typed are reported as different failures (N-6) because they
    are different mistakes: a client that never sent the key has to send it, and
    one that sent the wrong shape has to fix it. Collapsing the two into one
    message leaves a client unable to act on the correction.

    One function for both required keys rather than the check spelled out per
    key, because the two reasons and the error wrapped around them have to agree
    across keys. When each key owned the sentence, either could stop
    distinguishing the cases, or stop matching the other, with nothing failing.

    ``label`` is the type as the spec names it -- "a string", "an object" -- and
    not the Python class, because this string goes on the wire and
    ``must be dict`` is not what a client author is shown.
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
    # Both are checked for presence and shape here. Neither value is returned:
    # the router re-reads the version to compare it against the header, so
    # handing it back would be a second spelling of what the caller already has.
    _require_meta_key(meta, META_PROTOCOL_VERSION, str, "a string", request_id)
    _require_meta_key(meta, META_CLIENT_CAPABILITIES, dict, "an object", request_id)


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
