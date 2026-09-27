"""The webhook delivery path's HTTP answer, staged as a real httpx exchange.

``deliver_pinned_webhook`` builds its own client -- ``async with
httpx.AsyncClient(timeout=...) as client: async with client.stream("POST",
connect_url, ...) as response:`` -- and then drains the body through the
bounded-io cap. Two files were each standing a fake client in front of that:
a class with an ``__aenter__``, a ``stream()`` returning a context manager of
its own invention, and an ``aiter_bytes()`` that yielded a ``bytes`` object.

That double was a second httpx. It could not fail when production changed how
it read a response, which is the only thing a transport seam is for, and it
made the response-cap test assert against a fake iterator rather than the real
``read_capped_async`` over a real stream.

``record_webhook_delivery`` replaces rather than imitates. It patches the
``AsyncClient`` *constructor* so production still constructs and closes a real
client, and the real client is handed an ``httpx.MockTransport`` -- the seam
httpx itself publishes. What the test gets back is the list of real
``httpx.Request`` objects the delivery actually built, and everything in front
of the answer stays production: the timeout object, the request line, the
streaming read, the cap, the close on the way out.
"""

from __future__ import annotations

import httpx
from _transport_doubles import MockResponse

__all__ = ["record_webhook_delivery"]


def record_webhook_delivery(
    monkeypatch,
    *,
    body: bytes = b"",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> list[httpx.Request]:
    """Answer every webhook POST with ``body``; return the requests delivery built.

    ``body`` is the *response* body, which is attacker-controlled: a test that
    is about the response cap passes one bigger than the cap and a test that is
    about the request passes a small one (or the default empty body, which
    drains nothing). The recorded requests are the assertion surface for
    everything about the request -- that the connect URL is the pinned IP, that
    the ``Host`` header still carries the logical hostname, that an https
    delivery was not rewritten to an IP at all.
    """
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def _answer(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return MockResponse(status_code, headers=headers, content=body)

    transport = httpx.MockTransport(_answer)

    def _scripted_client(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _scripted_client)
    return requests
