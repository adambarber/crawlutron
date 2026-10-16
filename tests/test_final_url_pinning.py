"""A plain-HTTP fetch must never leak the pinned IP: the pin rewrites the wire
host to the resolved IP while preserving the logical URL, so final_url, cookie
keying and link-base must all reflect the ORIGINAL hostname, never the IP.
"""

import asyncio
from unittest.mock import patch

from app.transport import http_client_pool as pool_mod
from app.transport.fetchers import default_http_fetcher
from tests._policy_factories import canonical


class _FakeStream:
    def __init__(self, url, status=200, headers=None, body=b"<html></html>"):
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aiter_raw(self):
        yield self._body


def test_final_url_uses_hostname_not_pinned_ip():
    pinned = "http://93.184.216.34:80/page"
    fake = _FakeStream(pinned)
    with patch.object(pool_mod, "get_async_http_client") as gc:
        gc.return_value.stream.return_value = fake
        result = asyncio.run(
            default_http_fetcher(
                canonical("http://example.com/page"),
                {},
                5,
                1_000_000,
                pin=lambda url, headers: (pinned, {"Host": "example.com"}),
            )
        )
    assert result.final_url.raw == "http://example.com/page"
    assert "93.184.216.34" not in result.final_url.raw
