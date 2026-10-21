"""The pinned-request composition contract: one builder, every transport."""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from app.orchestration.classification import classify_error_code
from app.transport.pinned_transport import (
    DNS_WAIT_SECONDS,
    apinned_hops,
    build_pinned_request,
    dns_wait_for,
)
from tests._assertions import block_private_targets
from tests._policy_factories import canonical

# The scripted resolver answers a public IPv4 host; the builder resolves the hostname itself, so the test
# scripts the resolver rather than the network.
_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


def _resolve_as(addrs):
    return patch("socket.getaddrinfo", return_value=addrs)


class TestBuildPinnedRequest:
    """The builder applies the pin policy so a fetcher cannot re-resolve at connect.

    Plain HTTP connects to the validated IP with hostname in Host, HTTPS keeps
    the hostname; the old ``SafeUrl.connect`` refused an unpinned URL.
    """

    def test_plain_http_uses_pinned_ip_and_host_header(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            connect_url, headers = build_pinned_request(canonical("http://example.com:8080/a/b?x=1"), {})
        parsed = urlparse(connect_url)
        assert parsed.hostname == "93.184.216.34"
        assert parsed.port == 8080
        assert parsed.path == "/a/b"
        assert parsed.query == "x=1"
        assert headers["Host"] == "example.com:8080"

    def test_https_now_pins_to_the_ip_and_keeps_host_header(self, monkeypatch):
        block_private_targets(monkeypatch)
        https_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with _resolve_as(https_addrs):
            connect_url, headers = build_pinned_request(canonical("https://example.com/secure.pdf"), {})
        parsed = urlparse(connect_url)
        # HTTPS must be IP-pinned like HTTP so a DNS rebind cannot redirect it; the
        # original hostname is preserved for TLS SNI (httpx) and as the Host header.
        assert parsed.hostname == "93.184.216.34"
        assert parsed.scheme == "https"
        assert parsed.path == "/secure.pdf"
        assert headers["Host"] == "example.com"

    def test_plain_http_brackets_ipv6_literal(self, monkeypatch):
        # getaddrinfo can hand back a public IPv6 address; an unbracketed literal corrupts the URL netloc (its
        # colons collide with port parsing) and urllib misroutes. Pinned URLs must bracket v6 literals.
        block_private_targets(monkeypatch)
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 80, 0, 0))]
        with _resolve_as(v6_addrs):
            connect_url, headers = build_pinned_request(canonical("http://v6.example/docs.pdf?x=1"), {})
        parsed = urlparse(connect_url)
        assert connect_url.startswith("http://[2606:4700:4700::1111]/")
        assert parsed.hostname == "2606:4700:4700::1111"
        assert parsed.port is None
        assert parsed.path == "/docs.pdf"
        assert parsed.query == "x=1"
        assert headers["Host"] == "v6.example"


class TestOneResolutionPerHop:
    """#383: the fresh pin IS the SSRF validation, so the builder must not
    resolve the host a second time through the cached check."""

    @pytest.mark.parametrize("scheme", ("http", "https"))
    def test_pin_path_resolves_once_per_hop(self, monkeypatch, scheme):
        block_private_targets(monkeypatch)
        calls = {"n": 0}

        def fake(host, *args, **kwargs):
            calls["n"] += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        monkeypatch.setattr(socket, "getaddrinfo", fake)
        connect_url, _headers = build_pinned_request(canonical(f"{scheme}://example.com/a"), {})
        assert calls["n"] == 1, calls
        assert connect_url == f"{scheme}://93.184.216.34/a"


class TestResolverDeadline:
    """#383: a resolver that sleeps past the deadline must surface a
    caller-classifiable timeout, not an indefinite hang off the pool."""

    async def test_slow_resolver_surfaces_a_classifiable_timeout(self, monkeypatch):
        block_private_targets(monkeypatch)
        stall = threading.Event()

        def slow_resolver(host, *args, **kwargs):
            stall.wait(1.0)  # far past the budget the request hands the walk
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        monkeypatch.setattr(socket, "getaddrinfo", slow_resolver)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await anext(
                apinned_hops(
                    canonical("http://slow.example/"),
                    {},
                    pin=build_pinned_request,
                    timeout_seconds=0.3,
                )
            )
        assert time.monotonic() - started < 0.9  # bounded, not the resolver's full stall
        # The surfaced type is one the classification table maps.
        assert classify_error_code(TimeoutError("dns")) == "connection_error"

    async def test_a_resolver_within_the_budget_still_resolves(self, monkeypatch):
        """The budget cuts both ways: a resolver that answers inside the request's
        budget resolves, even though the posture alone would have cut it off."""
        block_private_targets(monkeypatch)
        stall = threading.Event()

        def slowish_resolver(host, *args, **kwargs):
            stall.wait(0.4)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        monkeypatch.setattr(socket, "getaddrinfo", slowish_resolver)
        chain, pinned_url, _hop_headers = await anext(
            apinned_hops(
                canonical("http://ok.example/"),
                {},
                pin=build_pinned_request,
                timeout_seconds=2,
            )
        )
        assert pinned_url == "http://93.184.216.34/"
        assert chain.url == canonical("http://ok.example/")

    def test_the_derivation_never_exceeds_the_posture(self):
        """The request budget tightens the wait; the posture is the ceiling."""
        assert dns_wait_for(0.3) == 0.3
        assert dns_wait_for(2) == 2
        assert dns_wait_for(30) == DNS_WAIT_SECONDS
        assert dns_wait_for(None) == DNS_WAIT_SECONDS
