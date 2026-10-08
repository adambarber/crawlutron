"""The pinned-request composition contract: one builder, every transport."""

import socket
from unittest.mock import patch
from urllib.parse import urlparse

from app.transport.pinned_transport import build_pinned_request
from tests._assertions import block_private_targets
from tests._policy_factories import canonical

# The scripted resolver answers a public IPv4 host; the builder resolves the
# hostname itself, so the test scripts the resolver rather than the network.
_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


def _resolve_as(addrs):
    return patch("socket.getaddrinfo", return_value=addrs)


class TestBuildPinnedRequest:
    """The builder applies the pin policy so a fetcher cannot re-resolve at
    connect time (DNS rebinding TOCTOU): plain HTTP connects to the exact
    validated IP with a Host header preserving the hostname; HTTPS keeps the
    hostname (SNI/cert), matching the sibling HTTP fetcher.

    The old ``SafeUrl.connect`` refused an unpinned URL; the builder removed
    the unpinned state entirely -- there is no ``pin`` switch left to leave off.
    """

    def test_plain_http_uses_pinned_ip_and_host_header(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            connect_url, headers = build_pinned_request(
                canonical("http://example.com:8080/a/b?x=1"), {}
            )
        parsed = urlparse(connect_url)
        assert parsed.hostname == "93.184.216.34"
        assert parsed.port == 8080
        assert parsed.path == "/a/b"
        assert parsed.query == "x=1"
        assert headers["Host"] == "example.com:8080"

    def test_https_keeps_hostname_for_sni(self, monkeypatch):
        block_private_targets(monkeypatch)
        https_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with _resolve_as(https_addrs):
            connect_url, headers = build_pinned_request(
                canonical("https://example.com/secure.pdf"), {}
            )
        parsed = urlparse(connect_url)
        assert parsed.hostname == "example.com"  # never the IP (breaks SNI)
        assert "Host" not in headers

    def test_plain_http_brackets_ipv6_literal(self, monkeypatch):
        # getaddrinfo can hand back a public IPv6 address; an unbracketed
        # literal corrupts the URL netloc (its colons collide with port
        # parsing) and urllib misroutes. Pinned URLs must bracket v6 literals.
        block_private_targets(monkeypatch)
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 80, 0, 0))]
        with _resolve_as(v6_addrs):
            connect_url, headers = build_pinned_request(
                canonical("http://v6.example/docs.pdf?x=1"), {}
            )
        parsed = urlparse(connect_url)
        assert connect_url.startswith("http://[2606:4700:4700::1111]/")
        assert parsed.hostname == "2606:4700:4700::1111"
        assert parsed.port is None
        assert parsed.path == "/docs.pdf"
        assert parsed.query == "x=1"
        assert headers["Host"] == "v6.example"
