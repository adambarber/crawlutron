"""#619: a declared Content-Length that never arrives is truncation, not a short
body -- the middleware must answer 413 instead of processing the partial body.
"""

import json

from starlette.responses import Response

from crawlutron_rest.api.middleware import limit_request_body
from tests._asgi_request import make_request, pass_through

CAP = 100


async def test_declared_over_received_is_413_not_processed():
    """Content-Length declared, fewer bytes streamed: the request is refused 413
    and the app never processes a body the client did not finish sending."""
    messages = [
        {"type": "http.request", "body": b"AB", "more_body": True},
        {"type": "http.request", "body": b"", "more_body": False},
    ]
    request = make_request(messages, [(b"content-length", b"50")])

    async def call_next(req):
        raise AssertionError("a truncated body must never reach the app")

    response = await limit_request_body(request, call_next, max_bytes=CAP)
    assert response.status_code == 413


async def test_declared_equal_to_received_is_processed():
    """An exactly-delivered declared body is not truncation: the app runs."""
    messages = [
        {"type": "http.request", "body": b"AB", "more_body": False},
    ]
    request = make_request(messages, [(b"content-length", b"2")])
    ran = []

    async def call_next(req):
        ran.append(req)
        return Response("ok")

    response = await limit_request_body(request, call_next, max_bytes=CAP)
    assert response.status_code == 200
    assert ran, "an honest body was refused"


async def test_truncation_response_is_the_error_envelope():
    """The 413 names the limit like the oversized path does, so one client
    handles both."""
    messages = [
        {"type": "http.request", "body": b"A", "more_body": False},
    ]
    request = make_request(messages, [(b"content-length", b"999")])
    response = await limit_request_body(request, pass_through, max_bytes=CAP)
    payload = json.loads(bytes(response.body))
    assert payload["code"] == "request_too_large"
    assert payload["details"] == {"max_bytes": CAP}
