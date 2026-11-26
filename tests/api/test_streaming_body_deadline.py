"""A declared body above the buffer window must honour its streaming deadline (#1486)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from starlette.responses import Response

import crawlutron_rest.api.middleware as mw
from crawlutron_rest.api.middleware import limit_request_body
from tests._asgi_request import make_request

DECLARED = 64 * 1024
MAX_BYTES = 16 * 1024 * 1024
TICKS = [0.0, 5.0, 5.0, 100.0]


def _stream_request(receive):
    """Declare more than the buffer window so receive() takes the streaming path."""
    return make_request([], headers=[(b"content-length", str(DECLARED).encode())], receive=receive)


async def _drain_to_end(request):
    while True:
        message = await request.receive()
        if not message.get("more_body", False):
            break


def _draining_app():
    async def call_next(request):
        await _drain_to_end(request)
        return Response("")

    return call_next


async def _run(receive, call_next):
    request = _stream_request(receive)
    return await asyncio.wait_for(limit_request_body(request, call_next, max_bytes=MAX_BYTES), timeout=5.0)


async def test_deadline_expiry_refuses_streamed_body(monkeypatch):
    """Kills mutant: removing the streaming deadline check (middleware.py:203)."""
    ticks = iter(TICKS)
    monkeypatch.setattr(mw, "time", SimpleNamespace(monotonic=lambda: next(ticks)))

    async def receive():
        return {"type": "http.request", "body": b"x" * 1024, "more_body": True}

    try:
        response = await _run(receive, _draining_app())
    except TimeoutError:
        pytest.fail("middleware.py:203: expired streaming deadline must answer 413 + Connection: close")
    assert response.status_code == 413, "middleware.py:203: expired streaming deadline must answer 413"
    assert response.headers["Connection"] == "close"


async def test_stalled_receive_hits_the_deadline(monkeypatch):
    """Kills mutant: removing the receive TimeoutError branch (middleware.py:206)."""
    monkeypatch.setattr(mw, "BODY_DEADLINE_SECONDS", 0.05)
    stalled = asyncio.Event()

    async def receive():
        await stalled.wait()
        return {"type": "http.request", "body": b"x", "more_body": True}

    response = await _run(receive, _draining_app())
    assert response.status_code == 413, "middleware.py:206: a stalled receive must answer 413"
    assert response.headers["Connection"] == "close"


async def test_non_request_message_refuses_streamed_body():
    """Kills mutant: forwarding a disconnect as body bytes (middleware.py:209)."""

    async def receive():
        return {"type": "http.disconnect"}

    response = await _run(receive, _draining_app())
    assert response.status_code == 413, "middleware.py:209: a non-request message must answer 413"
    assert response.headers["Connection"] == "close"
