"""The pinned-request composition contract: one builder, every transport."""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from app.transport.classification import classify_error_code
from app.transport.pinned_transport import (
    DNS_WAIT_SECONDS,
    apinned_hops,
    build_pinned_request,
    build_pinned_request_async,
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

    def test_host_header_brackets_resolved_ipv6(self, monkeypatch):
        # #495: the Host header composes the original v6 literal with a port; unbracketed
        # it reads as an ambiguous host:port (RFC 7230 5.4) and never as the literal.
        block_private_targets(monkeypatch)
        canonical("http://[2606:4700::1111]:8080/x")
        literal = canonical("http://[2606:4700::1111]:8080/x")
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 8080, 0, 0))]
        with _resolve_as(v6_addrs):
            _, headers = build_pinned_request(literal, {})
        assert headers["Host"] == "[2606:4700::1111]:8080"


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


def _stalling_resolver(monkeypatch, seconds: float, addrs=None):
    """Script the resolver to stall past a budget and answer ``addrs``; the Event
    keeps the wait real without tripping the suite's fast-sleep patch."""
    block_private_targets(monkeypatch)
    stall = threading.Event()

    def resolver(host, *args, **kwargs):
        stall.wait(seconds)
        return addrs if addrs is not None else _PUBLIC

    monkeypatch.setattr(socket, "getaddrinfo", resolver)
    return stall


class TestResolverDeadline:
    """#383: a resolver that sleeps past the deadline must surface a
    caller-classifiable timeout, not an indefinite hang off the pool."""

    async def test_slow_resolver_surfaces_a_classifiable_timeout(self, monkeypatch):
        _stalling_resolver(monkeypatch, 1.0)
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
        _stalling_resolver(monkeypatch, 0.4)
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


class TestWrapperBudget:
    """#383 follow-up: the off-loop executor wrapper derives its own wait from the
    request's budget -- the browser and webhook paths resolve through it, so a tight
    request tightens the wrapper too, not only the redirect-walk chain."""

    async def test_a_tight_budget_raises_promptly_off_the_pool(self, monkeypatch):
        _stalling_resolver(monkeypatch, 1.5)
        started = time.monotonic()
        with pytest.raises(TimeoutError):
            await build_pinned_request_async(canonical("http://slow.example/"), {}, timeout_seconds=0.2)
        assert time.monotonic() - started < 1.0  # the budget, not the 1.5s stall

    async def test_without_a_budget_the_wrapper_keeps_the_posture(self, monkeypatch):
        """No budget in hand means the DNS posture, not the caller's guess: a stall
        the posture tolerates still resolves."""
        _stalling_resolver(monkeypatch, 1.2)
        connect_url, _headers = await build_pinned_request_async(canonical("http://ok.example/"), {})
        assert urlparse(connect_url).hostname == "93.184.216.34"


class TestHeaderValueWireGuard:
    """#441: apinned_hops is the one edge both transports send through, so it is
    where a header VALUE that could end one header and start another (CR/LF/NUL)
    is refused -- whatever door it came through.
    """

    async def test_caller_crlf_value_is_refused_before_the_first_hop(self):
        # The resolver is scripted for the RED state: today the walk proceeds
        # toward the wire; once the guard lands the refusal precedes any resolve.
        with _resolve_as(_PUBLIC):
            with pytest.raises(ValueError):
                await anext(
                    apinned_hops(
                        canonical("http://example.com/"),
                        {"X-Tenant": "a\r\nCookie: admin=1"},
                        pin=build_pinned_request,
                    )
                )

    async def test_a_value_injected_by_a_pin_rule_is_refused_before_the_wire(self):
        """The pin rule is injectable by design; its output is re-checked, so a
        rule that hand-builds headers cannot smuggle past the edge either.
        """

        def injecting_rule(url, headers):
            out = dict(headers)
            out["X-Inject"] = "clean\r\nX-Smuggled: 1"
            return build_pinned_request(url, out)

        with _resolve_as(_PUBLIC):
            with pytest.raises(ValueError):
                await anext(apinned_hops(canonical("http://example.com/"), {}, pin=injecting_rule))

    async def test_clean_headers_still_reach_the_pin(self):
        # No false positives: ordinary values (multibyte included) pass untouched.
        with _resolve_as(_PUBLIC):
            _chain, _pinned_url, hop_headers = await anext(
                apinned_hops(canonical("http://example.com/"), {"X-Tenant": "Café"}, pin=build_pinned_request)
            )
        assert hop_headers["X-Tenant"] == "Café"
