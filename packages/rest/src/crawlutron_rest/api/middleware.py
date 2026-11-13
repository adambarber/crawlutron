"""ASGI middleware: body-size cap and the DNS-rebinding Origin guard.

``limit_request_body`` caps bodies at ``receive()`` and refuses a truncated
declared body (#619); ``reject_hostile_origin`` refuses a hostile browser Origin.
"""

from __future__ import annotations

import asyncio
import time

from crawlutron_mcp.constants import INVALID_REQUEST
from crawlutron_mcp.protocol import error_response
from crawlutron_mcp.server import origin_allowed
from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.types import Message, Receive

from crawlutron_rest.api.exceptions import error_envelope

MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

# Flat wall-clock bound on each body-handling loop so a slow-post client
# cannot pin a middleware task indefinitely.
BODY_DEADLINE_SECONDS = 30.0


async def reject_hostile_origin(request: Request, call_next):
    """Reject a present, non-loopback browser Origin (DNS-rebinding defense).

    Only a present but disallowed Origin is a 403; the shape follows the surface:
    /mcp answers with the JSON-RPC error object, every REST route the envelope.
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
            # The Origin header is attacker-controlled: the envelope names the
            # refusal, never echoing the caller's text back (#655).
            content=error_envelope("origin_not_allowed", "Origin not allowed"),
        )
    return await call_next(request)


async def _drain_declared_body(original_receive: Receive, *, max_bytes: int) -> None:
    """Discard a refused declared body up to ``max_bytes`` -- never unbounded.

    Past the cap the client is streaming forever or lying; the 413's
    Connection: close aborts the remainder. The deadline bounds slow-post holds.
    """
    drained = 0
    deadline = time.monotonic() + BODY_DEADLINE_SECONDS
    while drained < max_bytes:
        if time.monotonic() >= deadline:
            return
        try:
            message = await asyncio.wait_for(original_receive(), deadline - time.monotonic())
        except TimeoutError:
            return
        if not message.get("more_body", False):
            break
        drained += len(message.get("body", b"") or b"")


def _error_413(max_bytes: int) -> JSONResponse:
    """The one 413 the middleware answers, oversized or truncated alike."""
    return JSONResponse(
        status_code=413,
        content=error_envelope(
            "request_too_large",
            "Request body exceeds the maximum allowed size",
            {"max_bytes": max_bytes},
        ),
        headers={"Connection": "close"},
    )


async def _collect_declared_body(
    original_receive: Receive, *, declared: int, overflow: list[bool]
) -> list[Message] | None:
    """Verify a declared length arrives, buffering EXACTLY those bytes (#921).

    The buffer sums to the declared bytes and ends ``more_body: False``; ``None``
    is truncation the app must never process (#619). A crossing chunk is split.
    """
    buffered: list[Message] = []
    received = 0
    deadline = time.monotonic() + BODY_DEADLINE_SECONDS
    while received < declared:
        if time.monotonic() >= deadline:
            return None
        try:
            message = await asyncio.wait_for(original_receive(), deadline - time.monotonic())
        except TimeoutError:
            return None
        if message.get("type") != "http.request":
            # A non-request message mid-collect is a disconnect (uvicorn
            # re-delivers it forever): the body is truncated (#619), not spinnable.
            return None
        body = message.get("body", b"") or b""
        excess = (received + len(body)) - declared
        if excess > 0:
            keep = len(body) - excess
            buffered.append({**message, "body": body[:keep], "more_body": False})
            overflow[0] = True
            overflow[1] = bool(message.get("more_body", False))
            return buffered
        buffered.append(message)
        received += len(body)
        if received >= declared:
            return buffered
        if not message.get("more_body", False):
            return None
    return buffered


def _replaying_receive(
    buffered: list[Message], original_receive: Receive, *, max_bytes: int, overflow: list[bool]
) -> Receive:
    """Serve the buffered declared body, then the capped pass-through (#666);
    a body that overflowed its declaration (#921) yields nothing past the split."""
    replay = iter(buffered)
    declared = sum(len(message.get("body", b"") or b"") for message in buffered)
    if overflow[0]:
        # Split already ended the body (more_body False on the final message); a
        # further app receive must hand it nothing (never the over-declared bytes).
        empty = {"type": "http.request", "body": b"", "more_body": False}

        async def receive() -> Message:
            for message in replay:
                return message
            return empty

    else:
        capped = _capped_receive_for(original_receive, max_bytes=max_bytes - declared, truncated=[False])

        async def receive() -> Message:
            for message in replay:
                return message
            return await capped()

    return receive


def _capped_receive_for(original_receive: Receive, *, max_bytes: int, truncated: list[bool]):
    """The sticky cap for undeclared bodies: once truncating, every later
    receive hands the app nothing while the socket still drains."""
    bytes_delivered = 0

    async def receive() -> Message:
        nonlocal bytes_delivered, truncated
        message = await original_receive()
        if message.get("type") != "http.request":
            return message
        # The truncating chunk is not charged to bytes_delivered, so a later
        # receive must not read a positive budget from it and admit more.
        if truncated[0]:
            return {**message, "body": b"", "more_body": False}
        body = message.get("body", b"") or b""
        if not body:
            return message
        remaining = max_bytes - bytes_delivered
        if remaining <= 0:
            truncated[0] = True
            return {**message, "body": b"", "more_body": False}
        if len(body) > remaining:
            truncated[0] = True
            return {**message, "body": body[:remaining], "more_body": False}
        bytes_delivered += len(body)
        return message

    return receive


async def limit_request_body(request: Request, call_next, *, max_bytes: int = MAX_REQUEST_BODY_BYTES):
    """Cap declared request bodies; overflow is split, never routed (#921). #619
    truncation and #675 malformed length both refuse 413 like an oversize body."""
    original_receive = request.receive
    content_length = request.headers.get("content-length")
    conflicting = {
        value.decode("latin-1")
        for name, value in request.scope.get("headers", [])
        if name == b"content-length"
    }
    if len(conflicting) > 1:
        # Two distinct Content-Length values are a framing contradiction; drain
        # and refuse rather than serve whichever the parser happened to read.
        await _drain_declared_body(original_receive, max_bytes=max_bytes)
        return _error_413(max_bytes)
    declared = 0
    if content_length is not None:
        # A nonempty header that is not a plain non-negative decimal refuses
        # (#675): negative serves an empty body, non-int falls into truncation.
        try:
            declared = int(content_length)
        except ValueError:
            return _error_413(max_bytes)
        if not content_length.isascii() or not content_length.isdigit() or declared > max_bytes:
            # Mirror the truncation path: drain the refused body before the
            # 413, capped by bytes and the wall-clock deadline.
            await _drain_declared_body(original_receive, max_bytes=max_bytes)
            return _error_413(max_bytes)
    if declared:
        overflow: list[bool] = [False, False]
        buffered = await _collect_declared_body(original_receive, declared=declared, overflow=overflow)
        if buffered is None:
            return _error_413(max_bytes)
        request._receive = _replaying_receive(
            buffered, original_receive, max_bytes=max_bytes, overflow=overflow
        )
        response = await call_next(request)
        if overflow[0]:
            # Drain any body the split chunk flagged as still-coming (capped) so a
            # keep-alive reuse cannot read it as the next request (#921).
            if overflow[1]:
                await _drain_declared_body(original_receive, max_bytes=max_bytes)
            response.headers["Connection"] = "close"
        return response

    truncating: list[bool] = [False]
    request._receive = _capped_receive_for(original_receive, max_bytes=max_bytes, truncated=truncating)
    response = await call_next(request)
    if truncating[0]:
        response.headers["Connection"] = "close"
    return response


__all__ = ["BODY_DEADLINE_SECONDS", "MAX_REQUEST_BODY_BYTES", "limit_request_body", "reject_hostile_origin"]
