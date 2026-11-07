"""The webhook delivery path's HTTP answer, staged as a real httpx exchange: two
files each stood a fake second-httpx in front of ``deliver_pinned_webhook``, so
this patches the ``AsyncClient`` constructor to build a real client over ``httpx.MockTransport``.
"""

from __future__ import annotations

import httpx

from tests._transport_doubles import MockResponse

__all__ = ["record_webhook_delivery", "recorded_webhook_posts"]


def record_webhook_delivery(
    monkeypatch,
    *,
    body: bytes = b"",
    status_code: int = 200,
    headers: dict[str, str] | None = None,
) -> list[httpx.Request]:
    """Answer every webhook POST with ``body``; return the requests delivery built, for
    asserting the request side (pinned IP, Host header, no https-to-IP rewrite); the
    response body is attacker-controlled, so a cap test passes one over the cap.
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


def recorded_webhook_posts(monkeypatch):
    """Record only the webhook POSTs: the same transport double, filtered live so
    a crawl's own fetches (served by the real helper server) never enter the poll."""
    recorded = record_webhook_delivery(monkeypatch)

    class _WebhookPosts:
        """A live, filter-shaped view: reads narrow ``recorded`` to the webhook POSTs."""

        @staticmethod
        def _posts():
            return [r for r in recorded if "webhook.example.com" in str(r.url) and r.method == "POST"]

        def __iter__(self):
            return iter(self._posts())

        def __len__(self) -> int:
            return len(self._posts())

        def __getitem__(self, index: int):
            return self._posts()[index]

    return _WebhookPosts()


def assert_delivered_once(delivered) -> None:
    """Exactly one webhook was delivered, and it is the one under test; a zero- or
    two-delivery outcome used to raise ``IndexError`` or assert against the wrong
    one, so this names the delivery the following lines are about.
    """
    assert len(delivered) == 1, delivered
