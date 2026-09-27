"""SafeUrl chokepoint contract: validated state as the only representable state.

SSRF defense was five functions each call site had to compose correctly
(normalize, check, resolve-and-pin, assert-final, webhook-validate); every
redirect finding was "a path held a raw str and forgot a step." SafeUrl
folds normalize + check (+ pin) into parse(), and redirects are only
expressible via redirect_hop() -- there is no API surface for an
unvalidated hop.
"""

from __future__ import annotations

import socket
from unittest.mock import patch
from urllib.parse import urlparse

import pytest
from _assertions import block_private_targets
from _policy_factories import UNRESTRICTED_HOST_POLICY

from app.safe_url import SafeUrl

_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
_PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]


def _resolve_as(addrs):
    return patch("socket.getaddrinfo", return_value=addrs)


def _parse(url, **kwargs):
    return SafeUrl.parse(url, policy=UNRESTRICTED_HOST_POLICY, **kwargs)


class TestSafeUrlParse:
    def test_parse_public_url_validates(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = _parse("http://example.com/page")
        assert safe.hostname == "example.com"
        assert safe.pinned_ip is None
        assert safe.raw.startswith("http://example.com")

    def test_parse_private_url_raises(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PRIVATE), pytest.raises(ValueError):
            _parse("http://127.0.0.1:8080/admin")

    def test_parse_with_pin_returns_first_public_ip(self, monkeypatch):
        block_private_targets(monkeypatch)
        two_public = _PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        with _resolve_as(two_public):
            safe = _parse("http://example.com/", pin=True)
        assert safe.hostname == "example.com"
        assert safe.pinned_ip == "93.184.216.34"


@pytest.fixture
def public_url(monkeypatch):
    """A ``SafeUrl`` for a public page, under the private-target policy.

    Three of the hop tests are about where a *public* URL is allowed to go, and
    the two lines that establish that -- block private targets, resolve the host
    to a public address -- were written out in each. A hop test that forgot
    ``block_private_targets`` would pass for the wrong reason: nothing would
    have been pinned, so nothing would have been revalidated.
    """
    block_private_targets(monkeypatch)
    with _resolve_as(_PUBLIC):
        yield _parse("http://example.com/a")


class TestSafeUrlRedirectHop:
    def test_public_to_public_hop_revalidates(self, public_url):
        safe = public_url
        hopped = safe.redirect_hop("/b")
        assert hopped.hostname == "example.com"
        assert hopped.raw == "http://example.com/b"

    def test_public_to_private_hop_raises(self, public_url):
        with _resolve_as(_PRIVATE), pytest.raises(ValueError):
            public_url.redirect_hop("http://127.0.0.1:8080/evil")

    def test_hop_preserves_pin_preference(self, monkeypatch):
        # Not the public_url fixture: this one is about the pin surviving the
        # hop, so the hop has to happen while the public resolver is still
        # installed -- a hop performed outside it re-resolves for real.
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = _parse("http://example.com/a", pin=True)
            hopped = safe.redirect_hop("/b")
        assert hopped.pinned_ip == "93.184.216.34"


class TestSafeUrlConnect:
    """connect() applies the pin policy so a fetcher cannot re-resolve at
    connect time (DNS rebinding TOCTOU): plain HTTP connects to the exact
    validated IP with a Host header preserving the hostname; HTTPS keeps the
    hostname (SNI/cert), matching the sibling HTTP fetcher."""

    def test_http_connect_uses_pinned_ip_and_host_header(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = _parse("http://example.com:8080/a/b?x=1", pin=True)
        connect_url, headers = safe.connect()
        parsed = urlparse(connect_url)
        assert parsed.hostname == "93.184.216.34"
        assert parsed.port == 8080
        assert parsed.path == "/a/b"
        assert parsed.query == "x=1"
        assert headers["Host"] == "example.com:8080"

    def test_http_connect_refuses_unpinned_url(self, monkeypatch):
        # An unpinned SafeUrl has no validated IP to connect to; falling back
        # to hostname would silently re-open the rebinding window, so the
        # chokepoint refuses rather than degrading to the unsafe path.
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = _parse("http://example.com/a")  # pin defaults False
        with pytest.raises(ValueError):
            safe.connect()

    def test_https_connect_keeps_hostname_for_sni(self, monkeypatch):
        block_private_targets(monkeypatch)
        https_addrs = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with _resolve_as(https_addrs):
            safe = _parse("https://example.com/secure.pdf", pin=True)
        connect_url, headers = safe.connect()
        parsed = urlparse(connect_url)
        assert parsed.hostname == "example.com"  # never the IP (breaks SNI)
        assert "Host" not in headers

    def test_http_connect_brackets_ipv6_literal(self, monkeypatch):
        # H5-C: getaddrinfo can hand back a public IPv6 address; an unbracketed
        # literal corrupts the URL netloc (its colons collide with port
        # parsing) and urllib misroutes. Pinned URLs must bracket v6 literals.
        block_private_targets(monkeypatch)
        v6_addrs = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 80, 0, 0))]
        with _resolve_as(v6_addrs):
            safe = _parse("http://v6.example/docs.pdf?x=1", pin=True)
        connect_url, headers = safe.connect()
        parsed = urlparse(connect_url)
        assert connect_url.startswith("http://[2606:4700:4700::1111]/")
        assert parsed.hostname == "2606:4700:4700::1111"
        assert parsed.port is None
        assert parsed.path == "/docs.pdf"
        assert parsed.query == "x=1"
        assert headers["Host"] == "v6.example"
