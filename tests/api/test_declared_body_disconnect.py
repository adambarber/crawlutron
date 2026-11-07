"""#619 fix round: a re-delivered http.disconnect mid-collect must terminate
the middleware with a 413, and a slow under-cap drip must still be delivered.
"""

import asyncio

import pytest
from starlette.responses import Response

from crawlutron.api.middleware import limit_request_body
from tests._asgi_request import make_request


def _disconnecting_receive(disconnect_after: int):
    """A receive yielding that many body chunks, then re-delivering
    http.disconnect forever -- uvicorn's post-disconnect behavior."""
    count = [0]

    async def receive():
        index = count[0]
        count[0] += 1
        if index < disconnect_after:
            return {"type": "http.request", "body": b"X" * 10, "more_body": True}
        return {"type": "http.disconnect"}

    return receive


def _refusing_call_next(req):
    raise AssertionError("a disconnected body must never reach the app")


async def _answer_disconnected_request(disconnect_after: int):
    """The one disconnect arrange: a declared 1000-byte body whose receive
    yields that many chunks, then re-delivers http.disconnect forever."""
    request = make_request([], headers=[(b"content-length", b"1000")])
    request._receive = _disconnecting_receive(disconnect_after=disconnect_after)
    return await asyncio.wait_for(limit_request_body(request, _refusing_call_next), timeout=2.0)


async def test_disconnect_mid_collect_answers_413_and_terminates():
    """http.disconnect mid-body is truncation: 413, the app never runs, and the
    middleware task terminates -- a re-delivered disconnect must not spin the
    collector forever (#619)."""
    response = await _answer_disconnected_request(disconnect_after=3)
    assert response.status_code == 413


async def test_slow_declared_body_under_the_cap_is_delivered_intact():
    """A slow drip under the cap is honesty, not truncation: the app eventually
    receives every declared byte. Event-ordered assertions, never wall time."""
    drip = [
        {"type": "http.request", "body": b"A" * 5, "more_body": True},
        {"type": "http.request", "body": b"B" * 5, "more_body": True},
        {"type": "http.request", "body": b"C" * 5, "more_body": False},
    ]
    request = make_request(drip, headers=[(b"content-length", b"15")])
    seen = {}

    async def call_next(req):
        body = bytearray()
        while True:
            message = await req.receive()
            body += message.get("body", b"")
            if not message.get("more_body", False):
                break
        seen["body"] = bytes(body)
        return Response("ok")

    response = await asyncio.wait_for(limit_request_body(request, call_next), timeout=2.0)
    assert response.status_code == 200
    assert seen["body"] == b"AAAAABBBBBCCCCC", seen["body"]


@pytest.mark.parametrize("chunks_before_disconnect", [0, 1, 3])
async def test_disconnect_at_any_point_is_refused(chunks_before_disconnect):
    """The disconnect may arrive before any body or mid-stream; every position
    is truncation and must answer 413 without hanging."""
    response = await _answer_disconnected_request(disconnect_after=chunks_before_disconnect)
    assert response.status_code == 413
