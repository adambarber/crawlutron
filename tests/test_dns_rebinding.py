"""The proxy resolves once and dials the validated IP; the app carries only the
logical URL, so no second resolution can rebind a hop to a private address.
"""

from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest

from crawlutron.transport.fetchers import default_http_fetcher
from tests._assertions import (
    assert_dns_pinning_uses_first_resolution,
    block_private_targets,
    host_resolves_to,
)
from tests._fetcher_factories import MockResponse, scripted_transport_client
from tests._policy_factories import canonical


class TestDNSRebindingProtection:
    def test_a_mixed_public_private_answer_is_refused(self, monkeypatch):
        """Re-ruled #525 SEC-07: #404's first-public-wins drifted from
        resolve_and_pin_host (fail-closed on any private) and from the browser
        path (#387, mixed refused). One verdict per hostname: refuse."""
        import socket

        from crawlutron.transport.pinned_dns import resolve_and_pin_host

        def mixed_getaddrinfo(host, *args, **kwargs):
            return [
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80)),
                (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80)),
            ]

        with (
            host_resolves_to(monkeypatch, "unused", resolver=mixed_getaddrinfo),
            pytest.raises(ValueError, match="private"),
        ):
            resolve_and_pin_host(canonical("http://example.com/"))

    def test_resolve_and_pin_host_uses_first_resolution(self, monkeypatch):
        """Even if a second resolution would rebound to a private IP, the pinned
        IP (first resolution) is what the connection uses."""
        assert_dns_pinning_uses_first_resolution(monkeypatch, "169.254.169.254")

    async def test_fetcher_carries_the_logical_host_with_no_app_resolution(self, monkeypatch):
        """The fetcher must hand the logical host to the proxy and forward the
        original Host header; no app-side answer can rebind the hop."""
        block_private_targets(monkeypatch)
        # A real pooled client over a scripted transport: assert what httpx built.
        requested: list[httpx.Request] = []
        client = scripted_transport_client(
            [MockResponse(200, {"content-type": "text/html"}, b"<html></html>")],
            observed_requests=requested,
        )
        monkeypatch.setattr(
            "socket.getaddrinfo",
            lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("app DNS in fetch path")),
        )

        with patch("crawlutron.transport.http_client_pool.get_async_http_client", return_value=client):
            result = await default_http_fetcher(
                canonical("http://example.com/page"),
                {},
                10,
                50 * 1024 * 1024,
            )

        request = requested[0]
        parsed = urlparse(str(request.url))
        assert parsed.scheme == "http" and parsed.hostname == "example.com", request.url
        assert request.headers["Host"] == "example.com", request.headers
        assert result.status_code == 200
