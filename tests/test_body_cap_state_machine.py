"""The body-size middleware must stay truncated once the cap is breached: before,
a truncating chunk never charged ``bytes_delivered``, so a later ``receive()``
admitted more bytes -- ``truncated`` is now sticky and short-circuits each.
"""

import pytest
from starlette.requests import Request
from starlette.responses import Response

from app.api.middleware import MAX_REQUEST_BODY_BYTES, limit_request_body

CAP = 10


def _make_request(messages: list[dict[str, object]], headers: list[tuple[bytes, bytes]] | None = None):
    """A Request whose receive() pops the scripted ASGI message list."""

    async def receive():
        return messages.pop(0)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/",
        "headers": headers or [],
        "query_string": b"",
    }
    return Request(scope, receive=receive)


def _one_chunk(body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return _make_request(
        [{"type": "http.request", "body": body, "more_body": False}],
        headers=headers,
    )


async def _pass_through(request):
    return Response("")


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
def small_cap():
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

    await limit_request_body(request, call_next, max_bytes=small_cap)

    assert len(seen["body"]) == small_cap, len(seen["body"])
    assert seen["body"] == b"AAAA" + b"B" * (small_cap - 4)


async def test_drain_still_empties_the_client_stream(small_cap):
    """Regression guard for the property the removed flags encoded:
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

    response = await limit_request_body(request, call_next, max_bytes=small_cap)

    assert len(seen["body"]) == small_cap
    assert response.headers["Connection"] == "close"
    # The wrapper awaited original_receive() for every client chunk (drain)...
    assert messages == [], "undrained client chunks remain"
    # ...but handed the app nothing past the cap.
    assert seen["body"] == b"A" * small_cap


async def test_declared_body_over_the_cap_is_refused_before_the_app_sees_it():
    """A declared body over the cap costs nothing: the request never reaches the
    app and no receive is spent on a body that will be turned away."""
    request = _one_chunk(b"", headers=[(b"content-length", b"4096")])
    called = []

    async def call_next(req):
        called.append(req)
        return Response("")

    response = await limit_request_body(request, call_next, max_bytes=CAP)

    assert response.status_code == 413
    assert not called, "the app was invoked for a body already refused"


async def _refused_response(messages: list[dict[str, object]], *, declared: int) -> Response:
    """Drive the middleware with a declared-oversized body; the app must not run."""
    request = _make_request(messages, headers=[(b"content-length", str(declared).encode())])

    async def call_next(req):
        raise AssertionError("the app must not run for a body already refused")

    return await limit_request_body(request, call_next, max_bytes=CAP)


async def test_declared_body_over_cap_drains_only_up_to_the_cap():
    """The early Content-Length 413 drains the abandoned body so a keep-alive
    re-use cannot read it as the next request -- but no further than the cap:
    past it the client is pinned or lying, and Connection: close aborts it (#525)."""
    messages = [
        {"type": "http.request", "body": b"X" * 4, "more_body": True},
        {"type": "http.request", "body": b"X" * 4, "more_body": True},
        {"type": "http.request", "body": b"X" * 4, "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    response = await _refused_response(messages, declared=4096)

    assert response.status_code == 413
    assert response.headers["Connection"] == "close"
    assert len(messages) == 1, "the drain stopped at the cap, not at the declared 4096"


async def test_oversized_drain_is_capped_and_aborts_past_it():
    """The 413 drain must not read forever: past the cap it stops reading, and
    the Connection: close 413 aborts whatever the client still trickles."""
    messages = [{"type": "http.request", "body": b"X" * 64, "more_body": True} for _ in range(50)]
    response = await _refused_response(messages, declared=999999)

    assert response.status_code == 413
    assert response.headers["Connection"] == "close"
    assert len(messages) > 0, "the drain read past the cap instead of aborting"


@pytest.mark.parametrize("declared", [CAP, CAP - 1], ids=["at", "under"])
async def test_declared_body_within_the_cap_is_admitted(declared):
    """The pre-check rejects only what it says it rejects: a declared body at
    exactly the cap is not 'over' it."""
    request = _one_chunk(headers=[(b"content-length", str(declared).encode())])
    response = await limit_request_body(request, _pass_through, max_bytes=CAP)
    assert response.status_code == 200


async def test_refusal_reports_the_cap_it_enforced():
    """The 413 tells the client the limit it breached, or it is not actionable."""
    import json

    request = _one_chunk(headers=[(b"content-length", b"4096")])
    response = await limit_request_body(request, _pass_through, max_bytes=CAP)
    assert json.loads(bytes(response.body)) == {
        "code": "request_too_large",
        "message": "Request body exceeds the maximum allowed size",
        "details": {"max_bytes": CAP},
    }


async def test_unparsable_content_length_falls_through_to_the_wrapper():
    """A malformed CL header cannot be compared to a cap, and refusing on it
    would let a header decide. The wrapper is the enforcement, so the request
    proceeds and is truncated there instead."""
    request = _make_request(
        [
            {"type": "http.request", "body": b"Z" * 40, "more_body": False},
            {"type": "http.request", "body": b"", "more_body": False},
        ],
        headers=[(b"content-length", b"not-a-number")],
    )
    seen = {}

    async def call_next(req):
        seen["body"] = await _read_body_and_probe(req)
        return Response("")

    response = await limit_request_body(request, call_next, max_bytes=CAP)

    assert response.status_code == 200
    assert len(seen["body"]) == CAP


async def test_truncation_preserves_the_chunks_own_keys():
    """A chunk's metadata is forwarded verbatim; only its body is cut. A wrapper
    that rebuilt the message would drop the keys a streaming reader needs."""
    request = _make_request(
        [{"type": "http.request", "body": b"A" * 40, "more_body": False, "trailer": "x-checksum"}]
    )
    seen = {}

    async def call_next(req):
        seen["message"] = await req.receive()
        return Response("")

    await limit_request_body(request, call_next, max_bytes=CAP)

    assert seen["message"]["trailer"] == "x-checksum"
    assert seen["message"]["more_body"] is False


def test_default_cap_is_sixteen_mib():
    """The cap the app runs with, which no test here passes explicitly."""
    assert MAX_REQUEST_BODY_BYTES == 16 * 1024 * 1024


async def test_cap_is_enforced_on_the_app_that_serves_traffic():
    """Registration is what makes the cap real. A middleware nothing installs is
    a function that reads well and enforces nothing, and no unit test of it
    would ever say so -- so this drives the shipped app over its own stack."""
    from httpx import ASGITransport, AsyncClient

    from app.main import app

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        oversized = await http.post(
            "/v1/jobs",
            content=b"x" * (MAX_REQUEST_BODY_BYTES + 1),
            headers={"content-type": "application/json"},
        )
        within = await http.post(
            "/v1/jobs",
            content=b"{}",
            headers={"content-type": "application/json"},
        )

    assert oversized.status_code == 413, oversized.status_code
    # Not a 413 for every request: a cap that refuses everything is not a cap.
    assert within.status_code != 413, within.status_code
