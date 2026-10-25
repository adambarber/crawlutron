"""Tests that the DNS-rebinding (TOCTOU) fix actually pins the IP: before it, the
client re-resolved at connect time so a second response could return a private IP;
now ``resolve_and_pin_host`` resolves once and connects to the pinned IP.
"""

from unittest.mock import patch
from urllib.parse import urlparse

import httpx
import pytest

from app.transport.fetchers import default_http_fetcher
from tests._assertions import (
    assert_dns_pinning_uses_first_resolution,
    block_private_targets,
    host_resolves_to,
)
from tests._policy_factories import canonical
from tests._transport_doubles import MockResponse, scripted_transport_client


class TestDNSRebindingProtection:
    def test_a_mixed_public_private_answer_is_refused(self, monkeypatch):
        """Re-ruled #525 SEC-07: #404's first-public-wins drifted from
        check_ssrf_protection (fail-closed on any private) and from the browser
        path (#387, mixed refused). One verdict per hostname: refuse."""
        import socket

        from app.transport.pinned_transport import resolve_and_pin_host

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

    async def test_fetcher_connects_to_pinned_ip_with_original_host(self, monkeypatch):
        """The HTTP fetcher must connect to the pinned IP and forward the
        original Host header (so virtual-host routing is preserved)."""
        block_private_targets(monkeypatch)
        # A real pooled client over a scripted transport: what is asserted below is the request httpx actually
        # built, not a stand-in's account of it.
        requested: list[httpx.Request] = []
        client = scripted_transport_client(
            [MockResponse(200, {"content-type": "text/html"}, b"<html></html>")],
            observed_requests=requested,
        )

        with (
            host_resolves_to(monkeypatch, "1.2.3.4"),
            patch("app.transport.http_client_pool.get_async_http_client", return_value=client),
        ):
            result = await default_http_fetcher(
                canonical("http://example.com/page"),
                {},
                10,
                50 * 1024 * 1024,
            )

        request = requested[0]
        parsed = urlparse(str(request.url))
        assert parsed.scheme == "http" and parsed.hostname == "1.2.3.4", request.url
        assert request.headers["Host"] == "example.com", request.headers
        assert result.status_code == 200
