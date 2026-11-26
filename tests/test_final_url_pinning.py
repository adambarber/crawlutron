"""A plain-HTTP fetch must keep the logical hostname in final_url: the proxy
owns DNS and the dial, so no resolved IP ever reaches a result, cookie key or
link base.
"""

import asyncio
from unittest.mock import patch

from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
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

    async def aclose(self) -> None:
        return None


def test_final_url_uses_logical_hostname_not_a_resolved_ip():
    logical = "http://example.com/page"
    fake = _FakeStream(logical, headers={"content-type": "text/html"})

    with patch.object(pool_mod, "get_async_http_client") as gc:
        gc.return_value.stream.return_value = fake
        result = asyncio.run(
            default_http_fetcher(
                canonical(logical),
                {},
                5,
                1_000_000,
            )
        )
    assert result.final_url.raw == logical
    assert "93.184.216.34" not in result.final_url.raw
