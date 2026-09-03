"""ASGI body-size guard middleware.

Caps request bodies to 16 MiB at the ASGI ``receive()`` layer so a
malicious caller cannot OOM the container before pydantic validation.
Extracted from ``app/main.py`` so the FastAPI app factory stays thin
and the middleware is testable in isolation.

Behavior preserves the two adversarial-review fixes:

* **C1/C4** — truncated requests get ``Connection: close`` and the
  wrapper keeps draining ``original_receive()`` until ``more_body=False``
  so keep-alive is not poisoned.
* **C3** — per-chunk trailers are preserved via ``{**message, ...}``.
"""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import JSONResponse

_MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024


async def limit_request_body(request: Request, call_next):
    """ASGI middleware: cap request bodies at ``_MAX_REQUEST_BODY_BYTES``."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_REQUEST_BODY_BYTES:
                return JSONResponse(
                    status_code=413,
                    content={
                        "code": "request_too_large",
                        "message": "Request body exceeds the maximum allowed size",
                        "details": {"max_bytes": _MAX_REQUEST_BODY_BYTES},
                    },
                )
        except ValueError:
            pass
    original_receive = request.receive
    bytes_delivered = 0
    truncated = False
    drain_after_truncate = False
    already_drained = False

    async def _capped_receive() -> dict[str, object]:
        nonlocal bytes_delivered, truncated, drain_after_truncate, already_drained
        message = await original_receive()
        if message.get("type") != "http.request":
            return message
        body = message.get("body", b"") or b""
        if not body:
            return message
        if drain_after_truncate and not already_drained:
            already_drained = not message.get("more_body", False)
            return {**message, "body": b"", "more_body": False}
        remaining = _MAX_REQUEST_BODY_BYTES - bytes_delivered
        if remaining <= 0:
            truncated = True
            drain_after_truncate = True
            already_drained = not message.get("more_body", False)
            return {**message, "body": b"", "more_body": False}
        if len(body) > remaining:
            truncated = True
            drain_after_truncate = True
            already_drained = not message.get("more_body", False)
            return {**message, "body": body[:remaining], "more_body": False}
        bytes_delivered += len(body)
        return message

    request._receive = _capped_receive  # type: ignore[attr-defined]
    response = await call_next(request)
    if truncated:
        response.headers["Connection"] = "close"
    return response


__all__ = ["_MAX_REQUEST_BODY_BYTES", "limit_request_body"]
