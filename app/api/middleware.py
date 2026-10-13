"""ASGI body-size guard middleware.

Caps request bodies to 16 MiB at the ASGI ``receive()`` layer so a
malicious caller cannot OOM the container before pydantic validation.
Extracted from ``app/main.py`` so the app's construction in that module
stays thin, and so the middleware is testable in isolation.

Two properties of the cap are load-bearing:

* Truncated requests get ``Connection: close`` and the wrapper keeps
  draining ``original_receive()`` until ``more_body=False`` so keep-alive
  is not poisoned.
* Per-chunk trailers are preserved via ``{**message, ...}``.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from app.api.exceptions import error_envelope

MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


async def limit_request_body(request: Request, call_next, *, max_bytes: int = MAX_REQUEST_BODY_BYTES):
    """ASGI middleware: cap request bodies at ``max_bytes``."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > max_bytes:
                return JSONResponse(
                    status_code=413,
                    content=error_envelope(
                        "request_too_large",
                        "Request body exceeds the maximum allowed size",
                        {"max_bytes": max_bytes},
                    ),
                )
        except ValueError:
            pass
    original_receive = request.receive
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


__all__ = ["MAX_REQUEST_BODY_BYTES", "limit_request_body"]
