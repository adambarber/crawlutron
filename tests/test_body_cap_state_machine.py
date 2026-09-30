"""The body-size middleware must stay truncated once the cap is breached.

After truncating a chunk that carried ``more_body=False``, ``already_drained``
was set while ``bytes_delivered`` was never charged for the truncating chunk, so
a later ``receive()`` fell through to ``remaining = cap - bytes_delivered`` --
still positive -- and admitted more bytes after the app had already been told
the body ended. ``truncated`` is now sticky and short-circuits every later
receive at the top of the wrapper.
"""

import pytest
from starlette.requests import Request
from starlette.responses import Response

import app.api.middleware as middleware
from app.api.middleware import limit_request_body

CAP = 10


def _make_request(messages: list):
    """A Request whose receive() pops the scripted ASGI message list."""

    async def receive():
        return messages.pop(0)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
    return Request(scope, receive=receive)


async def _read_body_and_probe(request) -> bytes:
    """Read the body the app sees, then probe once more -- the shape of a
    streaming reader that checks for a trailing chunk after ``more_body``."""
    seen = bytearray()
    while True:
        message = await request.receive()
        seen += message.get("body", b"")
        if not message.get("more_body", False):
            break
    extra = await request.receive()
    seen += extra.get("body", b"")
    return bytes(seen)


@pytest.fixture()
def small_cap(monkeypatch):
    monkeypatch.setattr(middleware, "_MAX_REQUEST_BODY_BYTES", CAP)
    return CAP


async def test_no_bytes_admitted_after_truncating_final_chunk(small_cap):
    """The truncating chunk ends the body (more_body=False); a later receive()
    must not fall through to a still-positive ``remaining`` and hand over more."""
    request = _make_request(
        [
            {"type": "http.request", "body": b"AAAA", "more_body": True},
            {"type": "http.request", "body": b"B" * 24, "more_body": False},
            # The app's post-final probe carries a fresh oversized payload.
            {"type": "http.request", "body": b"C" * 24, "more_body": False},
        ]
    )
    seen = {}

    async def call_next(req):
        seen["body"] = await _read_body_and_probe(req)
        return Response("")

    await limit_request_body(request, call_next)

    assert len(seen["body"]) == small_cap, len(seen["body"])
    assert seen["body"] == b"AAAA" + b"B" * (small_cap - 4)


async def test_drain_still_empties_the_client_stream(small_cap):
    """Regression guard for the C1/C4 property the removed flags encoded:
    after truncation every receive() still awaits the underlying receive()
    (so a reading app keeps consuming the socket) and never admits a byte."""
    messages = [
        {"type": "http.request", "body": b"A" * 24, "more_body": True},
        {"type": "http.request", "body": b"B" * 24, "more_body": True},
        {"type": "http.request", "body": b"C" * 24, "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    request = _make_request(messages)
    seen = {}

    async def call_next(req):
        body = bytearray()
        # A draining reader keeps receiving until the client stream ends.
        while messages:
            message = await req.receive()
            body += message.get("body", b"")
        seen["body"] = bytes(body)
        return Response("")

    response = await limit_request_body(request, call_next)

    assert len(seen["body"]) == small_cap
    assert response.headers["Connection"] == "close"
    # The wrapper awaited original_receive() for every client chunk (drain)...
    assert messages == [], "undrained client chunks remain"
    # ...but handed the app nothing past the cap.
    assert seen["body"] == b"A" * small_cap
