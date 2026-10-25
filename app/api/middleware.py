"""ASGI middleware: body-size cap and the DNS-rebinding Origin guard.

``limit_request_body`` caps bodies at ``receive()``; ``reject_hostile_origin``
refuses a present non-loopback browser Origin on every surface.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.exceptions import error_envelope
from app.mcp.constants import INVALID_REQUEST
from app.mcp.protocol import error_response
from app.mcp.server import origin_allowed

MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


async def reject_hostile_origin(request: Request, call_next):
    """Reject a present, non-loopback browser Origin (DNS-rebinding defense).

    Only a present but disallowed Origin is a 403; the shape follows the surface:
    /mcp answers with the JSON-RPC error object, every REST route the envelope (#326).
    """
    origin = request.headers.get("origin")
    if not origin_allowed(origin):
        if request.url.path.startswith("/mcp"):
            return JSONResponse(
                status_code=403,
                content=error_response(None, INVALID_REQUEST, "Origin not allowed"),
            )
        return JSONResponse(
            status_code=403,
            content=error_envelope("origin_not_allowed", "Origin not allowed", {"origin": origin}),
        )
    return await call_next(request)


async def _drain_declared_body(original_receive, *, max_bytes: int) -> None:
    """Discard a refused declared body up to ``max_bytes`` -- never unbounded.

    Past the cap the client is streaming forever or lying; the 413's
    Connection: close aborts the remainder (#525, SEC-04).
    """
    drained = 0
    while drained <= max_bytes:
        message = await original_receive()
        if not message.get("more_body", False):
            break
        drained += len(message.get("body", b"") or b"")


async def limit_request_body(request: Request, call_next, *, max_bytes: int = MAX_REQUEST_BODY_BYTES):
    """ASGI middleware: cap request bodies at ``max_bytes``."""
    original_receive = request.receive
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                # Mirror the truncation path: drain the refused body before the
                # 413, capped so a slow-post client cannot pin the task (#525).
                await _drain_declared_body(original_receive, max_bytes=max_bytes)
                return JSONResponse(
                    status_code=413,
                    content=error_envelope(
                        "request_too_large",
                        "Request body exceeds the maximum allowed size",
                        {"max_bytes": max_bytes},
                    ),
                    headers={"Connection": "close"},
                )
        except ValueError:
            pass
    bytes_delivered = 0
    truncated = False

    async def _capped_receive() -> MutableMapping[str, Any]:
        nonlocal bytes_delivered, truncated
        message = await original_receive()
        if message.get("type") != "http.request":
            return message
            # The truncating chunk is not charged to bytes_delivered, so a later
            # receive must not read a positive budget from it and admit more.
        if truncated:
            return {**message, "body": b"", "more_body": False}
        body = message.get("body", b"") or b""
        if not body:
            return message
        remaining = max_bytes - bytes_delivered
        if remaining <= 0:
            truncated = True
            return {**message, "body": b"", "more_body": False}
        if len(body) > remaining:
            truncated = True
            return {**message, "body": body[:remaining], "more_body": False}
        bytes_delivered += len(body)
        return message

    request._receive = _capped_receive
    response = await call_next(request)
    if truncated:
        response.headers["Connection"] = "close"
    return response


__all__ = ["MAX_REQUEST_BODY_BYTES", "limit_request_body", "reject_hostile_origin"]
