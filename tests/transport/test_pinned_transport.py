"""The pinned-request composition contract: one builder, every transport."""

from __future__ import annotations

import socket
import threading
import time
from unittest.mock import patch
from urllib.parse import urlparse

import pytest

from crawlutron.transport.classification import classify_error_code
from crawlutron.transport.pinned_dns import DNS_WAIT_SECONDS, dns_wait_for
from crawlutron.transport.pinned_transport import (
    build_pinned_request,
    build_pinned_request_async,
)
from crawlutron.transport.redirect_walker import RedirectWalker
from tests._assertions import block_private_targets
from tests._fetch_doubles import make_fetch
from tests._policy_factories import canonical
from tests._transport_doubles import ScriptedTransport, _void_store_cookies

# The scripted resolver answers a public IPv4 host; the builder resolves the hostname itself, so the test
# scripts the resolver rather than the network.
_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]


def _resolve_as(addrs):
    return patch("socket.getaddrinfo", return_value=addrs)


def _resolve_https_as(addrs, monkeypatch):
    """The scripted public-HTTPS answer, with the guard forced on: the shared
    setup every pinned-HTTPS composition test stands on."""
    block_private_targets(monkeypatch)
    return _resolve_as([(socket.AF_INET, socket.SOCK_STREAM, 6, "", addrs)])


class TestBuildPinnedRequest:
    """The builder applies the pin policy so a fetcher cannot re-resolve at connect.

    Plain HTTP connects to the validated IP with hostname in Host, HTTPS keeps
    the hostname; the old ``SafeUrl.connect`` refused an unpinned URL.
    """

    def test_plain_http_uses_pinned_ip_and_host_header(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            request = build_pinned_request(canonical("http://example.com:8080/a/b?x=1"), {})
        assert request.connect_url == "http://93.184.216.34:8080/a/b?x=1"
        assert request.headers["Host"] == "example.com:8080"
        # No TLS identity to attest on plain HTTP.
        assert request.tls_hostname is None

    def test_https_now_pins_to_the_ip_and_keeps_host_header(self, monkeypatch):
        with _resolve_https_as(("93.184.216.34", 443), monkeypatch):
            request = build_pinned_request(canonical("https://example.com/secure.pdf"), {})
        assert request.connect_url == "https://93.184.216.34/secure.pdf"
        assert request.headers["Host"] == "example.com"
        # The identity rides the pin (#574): the hostname to attest travels with it.
        assert request.tls_hostname == "example.com"

    def test_an_ip_literal_pin_carries_no_tls_identity(self, monkeypatch):
        # The address IS the identity: no SNI and cert verification against the
        # literal itself. Re-deriving a hostname here would attest to a lie.
        with _resolve_https_as(("93.184.216.34", 443), monkeypatch):
            request = build_pinned_request(canonical("https://93.184.216.34/"), {})
        assert request.connect_url == "https://93.184.216.34/"
        assert request.tls_hostname is None

    def test_plain_http_brackets_ipv6_literal(self, monkeypatch):
        # getaddrinfo can hand back a public IPv6 address; an unbracketed literal corrupts the URL netloc (its
        # colons collide with port parsing) and urllib misroutes. Pinned URLs must bracket v6 literals.
        block_private_targets(monkeypatch)
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 80, 0, 0))]
        with _resolve_as(v6_addrs):
            request = build_pinned_request(canonical("http://v6.example/docs.pdf?x=1"), {})
        assert request.connect_url.startswith("http://[2606:4700:4700::1111]/")
        parsed = urlparse(request.connect_url)
        assert parsed.port is None
        assert parsed.path == "/docs.pdf"
        assert parsed.query == "x=1"
        assert request.headers["Host"] == "v6.example"

    def test_host_header_brackets_resolved_ipv6(self, monkeypatch):
        # #495: the Host header composes the original v6 literal with a port; unbracketed
        # it reads as an ambiguous host:port (RFC 7230 5.4) and never as the literal.
        block_private_targets(monkeypatch)
        canonical("http://[2606:4700::1111]:8080/x")
        literal = canonical("http://[2606:4700::1111]:8080/x")
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700::1111", 8080, 0, 0))]
        with _resolve_as(v6_addrs):
            request = build_pinned_request(literal, {})
        assert request.headers["Host"] == "[2606:4700::1111]:8080"


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
        request = build_pinned_request(canonical(f"{scheme}://example.com/a"), {})
        assert calls["n"] == 1, calls
        assert request.connect_url == f"{scheme}://93.184.216.34/a"


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
            await RedirectWalker(ScriptedTransport(), _void_store_cookies).walk(
                canonical("http://slow.example/"), {}, 0.3, pin=build_pinned_request
            )
        assert time.monotonic() - started < 0.9  # bounded, not the resolver's full stall
        # The surfaced type is one the classification table maps.
        assert classify_error_code(TimeoutError("dns")) == "connection_error"

    async def test_a_resolver_within_the_budget_still_resolves(self, monkeypatch):
        """The budget cuts both ways: a resolver that answers inside the request's
        budget resolves, even though the posture alone would have cut it off."""
        _stalling_resolver(monkeypatch, 0.4)
        transport = ScriptedTransport(
            [
                make_fetch(
                    status=200,
                    body=b"ok",
                    url="http://ok.example/",
                    final_url="http://ok.example/",
                )
            ]
        )
        result = await RedirectWalker(transport, _void_store_cookies).walk(
            canonical("http://ok.example/"), {}, 2, pin=build_pinned_request
        )
        assert result is not None and result.status_code == 200
        assert transport.calls[0][0].connect_url == "http://93.184.216.34/"
        assert result.url == canonical("http://ok.example/")

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
        request = await build_pinned_request_async(canonical("http://ok.example/"), {})
        assert urlparse(request.connect_url).hostname == "93.184.216.34"


class TestHeaderValueWireGuard:
    """#441: the redirect walk is the one edge both transports send through, so it is
    where a header VALUE that could end one header and start another (CR/LF/NUL)
    is refused -- whatever door it came through.
    """

    async def test_caller_crlf_value_is_refused_before_the_first_hop(self):
        # The refusal precedes any resolve: no DNS mock needed.
        with pytest.raises(ValueError):
            await RedirectWalker(ScriptedTransport(), _void_store_cookies).walk(
                canonical("http://example.com/"),
                {"X-Tenant": "a\r\nCookie: admin=1"},
                10,
                pin=build_pinned_request,
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
                await RedirectWalker(ScriptedTransport(), _void_store_cookies).walk(
                    canonical("http://example.com/"), {}, 10, pin=injecting_rule
                )

    async def test_clean_headers_still_reach_the_pin(self):
        # No false positives: ordinary values (multibyte included) pass untouched.
        transport = ScriptedTransport(
            [make_fetch(status=200, body=b"ok", url="http://example.com/", final_url="http://example.com/")]
        )
        with _resolve_as(_PUBLIC):
            await RedirectWalker(transport, _void_store_cookies).walk(
                canonical("http://example.com/"), {"X-Tenant": "Café"}, 10, pin=build_pinned_request
            )
        assert transport.calls[0][0].headers["X-Tenant"] == "Café"


class TestPinnedRequestIdentity:
    """#574: the pin travels WITH its TLS identity -- a transport receiving the
    pinned request receives the hostname to attest. The builder is the sole
    composer; no consumer re-derives identity from a connect URL."""

    async def test_the_identity_rides_the_off_loop_entry_and_the_walk(self, monkeypatch):
        with _resolve_https_as(("93.184.216.34", 443), monkeypatch):
            request = await build_pinned_request_async(canonical("https://example.com/"), {})
            transport = ScriptedTransport(
                [
                    make_fetch(
                        status=200,
                        body=b"ok",
                        url="https://example.com/",
                        final_url="https://example.com/",
                    )
                ]
            )
            await RedirectWalker(transport, _void_store_cookies).walk(
                canonical("https://example.com/"), {}, 10, pin=build_pinned_request
            )
        assert request.tls_hostname == "example.com"
        assert transport.calls[0][0].url.tls_hostname == "example.com"


def test_one_redirect_walk_on_the_surface():
    """One redirect walk on the surface: RedirectWalker. RedirectChain and the
    apinned_hops redirect door are gone, so one chain yields one verdict."""
    import crawlutron.transport.pinned_transport as pinned_mod

    assert not hasattr(pinned_mod, "RedirectChain")
    assert not hasattr(pinned_mod, "apinned_hops")
    assert not hasattr(pinned_mod, "apinned_stream")

    from crawlutron.transport.redirect_walker import RedirectWalker

    assert RedirectWalker.__name__ == "RedirectWalker"
