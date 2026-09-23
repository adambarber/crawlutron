"""H4 regression: a plain-HTTP fetch must never leak the pinned IP.

_build_pinned_request rewrites the wire URL's host to the resolved IP
(DNS-rebinding defense) while preserving the logical URL via Host. The
pinned IP is a transport detail: final_url, cookie keying, and link-base
resolution must all reflect the ORIGINAL hostname URL the caller asked
for -- never http://<resolved-ip>/..., which leaks resolved topology to
API consumers, fragments the cookie jar by IP, and mis-bases discovered
links.
"""

import asyncio
from unittest.mock import patch

from app.crawl.strategies import http_client_pool as pool_mod
from app.crawl.strategies import pinned_transport as pinned_mod
from app.crawl.strategies.fetchers import _default_http_fetcher
from app.validation import HostPolicy


class _FakeStream:
    def __init__(self, url, status=200, headers=None, body=b"<html></html>"):
        self._url = url
        self.url = url
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def aread(self):
        return self._body

    async def aiter_raw(self):
        yield self._body


def test_final_url_uses_hostname_not_pinned_ip():
    pinned = "http://93.184.216.34:80/page"
    fake = _FakeStream(pinned)
    with (
        patch.object(pinned_mod, "_build_pinned_request", return_value=(pinned, {"Host": "example.com"})),
        patch.object(pool_mod, "get_async_http_client") as gc,
    ):
        gc.return_value.stream.return_value = fake
        result = asyncio.run(_default_http_fetcher(
            "http://example.com/page",
            {},
            5.0,
            1_000_000,
            policy=HostPolicy.unrestricted(),
        ))
    assert result.final_url == "http://example.com/page"
    assert "93.184.216.34" not in result.final_url
