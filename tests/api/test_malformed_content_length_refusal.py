"""Malformed Content-Length headers refuse, never degrade (#675). A nonempty Content-Length that is not a
non-negative decimal integer is a malformed declaration: negative parses as int and serves an empty body,
non-int silently truncates. Both answer the truncation envelope, 413.
"""

from __future__ import annotations

import json

from starlette.requests import Request

from crawlutron_rest.api.middleware import limit_request_body
from tests._asgi_request import pass_through, refuse_appends_to, refuse_never, two_chunk_request

CAP = 100


def _declared_request(value: bytes, body: bytes = b"Z" * 40):
    """A request with a malformed Content-Length and a body chunk to drain."""
    return two_chunk_request([(b"content-length", value)], body=body)


async def test_negative_content_length_is_refused_never_empty_body():
    """Content-Length: -5 parses as int, passes the cap, and the declared collect loop runs zero times --
    serving an empty body as success. It must refuse like every other malformed declaration (drain first,
    like the oversized path).
    """
    request = _declared_request(b"-5")
    ran: list[Request] = []

    response = await limit_request_body(request, refuse_appends_to(ran), max_bytes=CAP)
    assert response.status_code == 413, response.status_code
    assert not ran, "a negative Content-Length served an empty body"


async def test_non_integer_content_length_is_refused_not_truncation():
    """Content-Length: abc is swallowed to declared=0 and falls into the
    undeclared truncation path -- a DIFFERENT wrong posture. It must refuse."""
    request = _declared_request(b"abc")

    response = await limit_request_body(
        request, refuse_never("a malformed Content-Length must never reach the app"), max_bytes=CAP
    )
    assert response.status_code == 413, response.status_code


async def test_malformed_content_length_answers_the_error_envelope():
    """One refusal shape for every malformed declaration: the truncation
    envelope, so a client handles negative, non-int, oversized, and
    short-delivered alike."""
    for value in (b"-1", b"abc", b"+7", b" 5", b"5_0"):
        request = _declared_request(value)
        response = await limit_request_body(request, pass_through, max_bytes=CAP)
        payload = json.loads(bytes(response.body))
        assert response.status_code == 413, (value, response.status_code)
        assert payload["code"] == "request_too_large", (value, payload)
