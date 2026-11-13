"""#921: a declared Content-Length bounds the body the app sees -- over-declared
chunks are split, the remainder drained, never routed (an HTTP framing vector).
Declared verifies delivery (#619); max_bytes is the hard bound (#666)."""

import json

from starlette.responses import Response

from crawlutron_rest.api.middleware import limit_request_body
from tests._asgi_request import body_reader_call_next, make_request, pass_through, refuse_never

CAP = 100


def _drain_terminator() -> list[dict[str, object]]:
    """The terminator an over-declared stream ends with, so the post-call drain
    consumes it and stops (mirrors the overflow-path drain)."""
    return [{"type": "http.request", "body": b"", "more_body": False}]


async def _split_delivery(messages: list[dict[str, object]], expected: bytes) -> Response:
    """Drive a Content-Length:10 body over-declared by ``messages``; the app must
    read exactly ``expected`` bytes (the split), never the overflow."""
    request = make_request(messages, headers=[(b"content-length", b"10")])
    seen: dict[str, bytes] = {}
    response = await limit_request_body(request, body_reader_call_next(seen), max_bytes=CAP)
    assert seen["body"] == expected, seen["body"]
    return response


async def test_one_chunk_over_declared_splits_extra_bytes():
    """Content-Length: 10 with one 100-byte chunk: the app receives exactly the
    10 declared bytes, never the extra 90 (the framing must not leak)."""
    response = await _split_delivery(
        [{"type": "http.request", "body": b"Y" * 100, "more_body": True}, *_drain_terminator()],
        b"Y" * 10,
    )
    assert response.status_code == 200
    assert response.headers["Connection"] == "close"


async def test_chunks_crossing_declared_split_to_exactly_declared():
    """Content-Length: 10 with chunks 5+10: the crossing chunk is split so the
    app gets exactly 10 bytes across the boundary, never 15."""
    response = await _split_delivery(
        [
            {"type": "http.request", "body": b"AAAAA", "more_body": True},
            {"type": "http.request", "body": b"BBBBBBBBBB", "more_body": False},
            *_drain_terminator(),
        ],
        b"AAAAA" + b"BBBBB",
    )
    assert response.status_code == 200


async def test_split_serves_nothing_past_the_declared_body():
    """After the declared body is replayed, further app receives hand it
    nothing -- the streaming reader's trailing probe must not surface the
    over-declared remainder."""
    await _split_delivery(
        [
            {"type": "http.request", "body": b"Q" * 30, "more_body": False},
            {"type": "http.request", "body": b"R" * 50, "more_body": False},
        ],
        b"Q" * 10,
    )


async def test_post_declared_bytes_flow_through_capped_pass_through():
    """Declared exactly met with more_body True: post-declared bytes flow
    through the capped pass-through up to max_bytes (#666) -- declared verifies
    delivery arrival, max_bytes bounds the streamed body. Not a desync."""
    request = make_request(
        [
            {"type": "http.request", "body": b"W" * 4, "more_body": True},
            {"type": "http.request", "body": b"K" * 24, "more_body": False},
            *_drain_terminator(),
        ],
        headers=[(b"content-length", b"4")],
    )
    seen: dict[str, bytes] = {}
    response = await limit_request_body(request, body_reader_call_next(seen), max_bytes=10)
    assert seen["body"] == b"W" * 4 + b"K" * 6, seen["body"]
    assert response.status_code == 200


async def test_overflow_drain_consumes_follower_before_next_request():
    """Removing the overflow drain leaves follower bytes on original_receive,
    so a subsequent keep-alive read would observe them (#921)."""
    sentinel = {"type": "http.request", "body": b"GET /next", "more_body": False}
    messages = [
        {"type": "http.request", "body": b"X" * 100, "more_body": True},
        {"type": "http.request", "body": b"Y" * 60, "more_body": True},
        {"type": "http.request", "body": b"Z" * 1, "more_body": False},
        sentinel,
    ]
    request = make_request(messages, headers=[(b"content-length", b"10")])
    response = await limit_request_body(request, pass_through, max_bytes=CAP)
    assert response.status_code == 200
    assert response.headers["Connection"] == "close"
    assert messages == [sentinel], f"follower not drained: {messages}"


def _conflicting_body(values: list[bytes], body: bytes = b"H" * 10):
    """Request with several Content-Length values and a body that WOULD succeed
    absent the conflict refusal: 10 bytes bounded by max_bytes for declared 10."""
    headers = [(b"content-length", value) for value in values]
    return make_request([{"type": "http.request", "body": body, "more_body": False}], headers=headers)


async def _conflict_envelope(values: list[bytes]):
    """The 413 envelope ``limit_request_body`` answers for conflicting values."""
    response = await limit_request_body(_conflicting_body(values), pass_through, max_bytes=CAP)
    return response, json.loads(bytes(response.body))


async def test_conflicting_content_length_headers_refuse_413():
    """Distinct Content-Length values on one request are a framing contradiction
    and must refuse 413, never serve whichever value the parser happened to pick."""
    response = await limit_request_body(
        _conflicting_body([b"10", b"20"]),
        refuse_never("a conflicting Content-Length must never reach the app"),
        max_bytes=CAP,
    )
    assert response.status_code == 413, response.status_code
    assert response.headers["Connection"] == "close"


async def test_conflicting_content_length_answers_the_error_envelope():
    """The conflicting-CL refusal shares the one 413 envelope with every other
    framing refusal, so a client handles it the same way."""
    response, payload = await _conflict_envelope([b"10", b"20"])
    assert response.status_code == 413, response.status_code
    assert payload["code"] == "request_too_large", payload


async def test_duplicate_identical_content_length_is_not_a_conflict():
    """A repeated identical Content-Length (defensive client) is not a framing
    contradiction; a body that still exceeds it refuses like any oversize."""
    response = await limit_request_body(
        make_request(
            [{"type": "http.request", "body": b"Z" * 200, "more_body": False}],
            headers=[(b"content-length", b"10"), (b"content-length", b"10")],
        ),
        pass_through,
        max_bytes=CAP,
    )
    assert response.status_code == 200, response.status_code
