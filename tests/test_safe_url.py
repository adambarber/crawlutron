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

import pytest
from _testkit import block_private_targets

from app.safe_url import SafeUrl

_PUBLIC = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
_PRIVATE = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]


def _resolve_as(addrs):
    return patch("socket.getaddrinfo", return_value=addrs)


class TestSafeUrlParse:
    def test_parse_public_url_validates(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = SafeUrl.parse("http://example.com/page")
        assert safe.hostname == "example.com"
        assert safe.pinned_ip is None
        assert safe.raw.startswith("http://example.com")

    def test_parse_private_url_raises(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PRIVATE), pytest.raises(ValueError):
            SafeUrl.parse("http://127.0.0.1:8080/admin")

    def test_parse_with_pin_returns_first_public_ip(self, monkeypatch):
        block_private_targets(monkeypatch)
        two_public = _PUBLIC + [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        with _resolve_as(two_public):
            safe = SafeUrl.parse("http://example.com/", pin=True)
        assert safe.hostname == "example.com"
        assert safe.pinned_ip == "93.184.216.34"


class TestSafeUrlRedirectHop:
    def test_public_to_public_hop_revalidates(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = SafeUrl.parse("http://example.com/a")
            hopped = safe.redirect_hop("/b")
        assert hopped.hostname == "example.com"
        assert hopped.raw == "http://example.com/b"

    def test_public_to_private_hop_raises(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = SafeUrl.parse("http://example.com/a")
        with _resolve_as(_PRIVATE), pytest.raises(ValueError):
            safe.redirect_hop("http://127.0.0.1:8080/evil")

    def test_hop_preserves_pin_preference(self, monkeypatch):
        block_private_targets(monkeypatch)
        with _resolve_as(_PUBLIC):
            safe = SafeUrl.parse("http://example.com/a", pin=True)
            hopped = safe.redirect_hop("/b")
        assert hopped.pinned_ip == "93.184.216.34"
