"""The body-size middleware must stay truncated once the cap is breached: before,
a truncating chunk never charged ``bytes_delivered``, so a later ``receive()``
admitted more bytes -- ``truncated`` is now sticky and short-circuits each.
"""

import pytest
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import Message

from crawlutron.api.middleware import MAX_REQUEST_BODY_BYTES, limit_request_body
from tests._asgi_request import (
    body_reader_call_next,
    make_request,
    pass_through,
    refuse_appends_to,
    refuse_never,
    two_chunk_request,
)

CAP = 10


def _one_chunk(body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None):
    return make_request(
        [{"type": "http.request", "body": body, "more_body": False}],
        headers=headers,
    )


def _one_declared_chunk(declared: int):
    """A body that delivers exactly what its Content-Length declares (#619)."""
    return _one_chunk(b"x" * declared, headers=[(b"content-length", str(declared).encode())])


@pytest.fixture()
def small_cap():
    return CAP


def _declared_then_stream(request_stream: list[dict[str, object]], declared: int):
    """A declared body plus the chunks that follow it -- the arrange the
    post-replay and truncation tests share."""
    return make_request(request_stream, headers=[(b"content-length", str(declared).encode())])


async def test_no_bytes_admitted_after_truncating_final_chunk(small_cap):
    """The truncating chunk ends the body (more_body=False); a later receive()
    must not fall through to a still-positive ``remaining`` and hand over more."""
    request = _declared_then_stream(
        [
            {"type": "http.request", "body": b"AAAA", "more_body": True},
            {"type": "http.request", "body": b"B" * 24, "more_body": False},
            # The app's post-final probe carries a fresh oversized payload.
            {"type": "http.request", "body": b"C" * 24, "more_body": False},
        ],
        declared=4,
    )
    seen: dict[str, bytes] = {}
    await limit_request_body(request, body_reader_call_next(seen), max_bytes=small_cap)
    delivered: bytes = seen["body"]
    assert len(delivered) == small_cap, len(delivered)
    assert delivered == b"AAAA" + b"B" * (small_cap - 4)


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
    request = make_request(messages)
    seen: dict[str, bytes] = {}

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
    called: list[Request] = []

    response = await limit_request_body(request, refuse_appends_to(called), max_bytes=CAP)

    assert response.status_code == 413
    assert not called, "the app was invoked for a body already refused"


async def _refused_response(messages: list[dict[str, object]], *, declared: int) -> Response:
    """Drive the middleware with a declared-oversized body; the app must not run."""
    request = make_request(messages, headers=[(b"content-length", str(declared).encode())])

    async def call_next(req):
        raise AssertionError("the app must not run for a body already refused")

    return await limit_request_body(request, call_next, max_bytes=CAP)


async def test_declared_body_over_cap_drains_only_up_to_the_cap():
    """The early Content-Length 413 drains the abandoned body so a keep-alive
    re-use cannot read it as the next request -- but no further than the cap:
    past it the client is pinned or lying, and Connection: close aborts it (#525)."""
    messages = [
        {"type": "http.request", "body": b"X" * 4, "more_body": True},
        *[{"type": "http.request", "body": b"X" * 4, "more_body": True} for _ in range(2)],
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
    request = _one_declared_chunk(declared)
    response = await limit_request_body(request, pass_through, max_bytes=CAP)
    assert response.status_code == 200


async def test_refusal_reports_the_cap_it_enforced():
    """The 413 tells the client the limit it breached, or it is not actionable."""
    import json

    request = _one_chunk(headers=[(b"content-length", b"4096")])
    response = await limit_request_body(request, pass_through, max_bytes=CAP)
    assert json.loads(bytes(response.body)) == {
        "code": "request_too_large",
        "message": "Request body exceeds the maximum allowed size",
        "details": {"max_bytes": CAP},
    }


async def test_unparsable_content_length_is_refused_with_the_malformed_posture():
    """A malformed CL header refuses 413 (#675 unification). OLD PIN (#619
    posture, deleted by the #675 order): fall through to the wrapper and
    truncate -- 'a malformed CL cannot be compared to a cap'. The #675 ticket
    names that swallow a DIFFERENT wrong posture (a negative CL serves an
    empty body, a non-int CL silently truncates) and orders ONE refusal shape
    for every malformed declaration -- the new pin is strictly stronger."""
    request = two_chunk_request([(b"content-length", b"not-a-number")])
    seen: dict[str, bytes] = {}

    response = await limit_request_body(
        request, refuse_never("a malformed Content-Length must never reach the app"), max_bytes=CAP
    )

    assert response.status_code == 413
    assert seen == {}, "the app ran for a refused request"


async def test_truncation_preserves_the_chunks_own_keys():
    """A chunk's metadata is forwarded verbatim; only its body is cut. A wrapper
    that rebuilt the message would drop the keys a streaming reader needs."""
    request = make_request(
        [{"type": "http.request", "body": b"A" * 40, "more_body": False, "trailer": "x-checksum"}]
    )
    seen: dict[str, Message] = {}

    async def call_next(req):
        seen["message"] = await req.receive()
        return Response("")

    await limit_request_body(request, call_next, max_bytes=CAP)

    message = seen["message"]
    assert message["trailer"] == "x-checksum"
    assert message["more_body"] is False


def test_default_cap_is_sixteen_mib():
    """The cap the app runs with, which no test here passes explicitly."""
    assert MAX_REQUEST_BODY_BYTES == 16 * 1024 * 1024


async def test_cap_is_enforced_on_the_app_that_serves_traffic():
    """Registration is what makes the cap real. A middleware nothing installs is
    a function that reads well and enforces nothing, and no unit test of it
    would ever say so -- so this drives the shipped app over its own stack."""
    from httpx import ASGITransport, AsyncClient

    from crawlutron.main import app

    assert app is not None, "the lifespan-set app is absent"
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


async def test_post_replay_pass_through_is_capped(small_cap):
    """After the buffered declared body replays, the live stream stays capped:
    a client that declared small and streams big cannot hand the app an
    uncapped firehose (#666)."""
    request = _declared_then_stream(
        [
            {"type": "http.request", "body": b"AB", "more_body": True},
            {"type": "http.request", "body": b"C" * 40, "more_body": True},
            {"type": "http.request", "body": b"D" * 40, "more_body": False},
        ],
        declared=2,
    )
    seen: dict[str, bytes] = {}
    await limit_request_body(request, body_reader_call_next(seen), max_bytes=small_cap)
    delivered = seen["body"]
    assert delivered == b"AB" + b"C" * (small_cap - 2) and len(delivered) == small_cap, delivered


async def test_drain_stops_at_the_cap_not_one_chunk_past(small_cap):
    """The 413 drain stops once the cap is drained: ``<=`` read one chunk past
    a drain that landed exactly on the cap (#666)."""
    # Cap 10: two 5-byte chunks land the drain exactly ON the cap; the third
    # 5-byte chunk is the one the old `<=` guard admitted.

    def chunk(body: bytes) -> dict[str, object]:
        return {"type": "http.request", "body": body, "more_body": True}

    messages = [chunk(b"X" * 5), chunk(b"Y" * 5), chunk(b"") | {"more_body": False}]
    response = await _refused_response(messages, declared=4096)

    assert response.status_code == 413
    assert len(messages) == 1, "the drain read past the cap it had reached"


async def _refused_within_deadline(request: Request, *, max_bytes: int) -> Response:
    """Drive the middleware against a refused body under the 1s outer bound:
    the deadline must answer 413 + close before the probe times out."""
    import asyncio

    async def probe():
        msg = "refused bodies never reach the app"
        return await limit_request_body(request, refuse_never(msg), max_bytes=max_bytes)

    response = await asyncio.wait_for(probe(), timeout=1.0)
    assert response.status_code == 413
    assert response.headers["Connection"] == "close"
    return response


@pytest.mark.real_sleeps
async def test_slow_drain_client_is_refused_within_the_deadline(monkeypatch):
    """A slow-post client that stalls inside an over-cap declared body must be
    refused with the 413 within the body deadline -- byte caps alone pin the
    middleware task forever when receive() never returns."""
    import asyncio

    import crawlutron.api.middleware as mw

    monkeypatch.setattr(mw, "BODY_DEADLINE_SECONDS", 0.05)

    stalled = asyncio.Event()

    async def stalled_receive() -> Message:
        await stalled.wait()
        raise AssertionError("unreachable: the deadline must fire first")

    request = make_request([], headers=[(b"content-length", b"4096")], receive=stalled_receive)
    await _refused_within_deadline(request, max_bytes=CAP)


@pytest.mark.real_sleeps
async def test_slow_collect_client_is_refused_within_the_deadline(monkeypatch):
    """A dribbling client that never delivers the declared length holds
    _collect open; the deadline refuses the truncated body instead of
    pinning the task."""
    import asyncio

    import crawlutron.api.middleware as mw

    monkeypatch.setattr(mw, "BODY_DEADLINE_SECONDS", 0.05)

    async def endless_dribble() -> Message:
        await asyncio.sleep(0.01)
        return {"type": "http.request", "body": b"X", "more_body": True}

    request = make_request([], headers=[(b"content-length", b"100")], receive=endless_dribble)
    await _refused_within_deadline(request, max_bytes=1024)
