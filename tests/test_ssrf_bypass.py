"""Tests that SSRF protection blocks private/loopback targets.

Uses ``monkeypatch`` to disable the local-dev escape hatch per test so the
global environment is always restored and never poisons other tests.
"""
import socket
from unittest.mock import patch

import pytest

from app.crawl.strategies.http import _check_ssrf_protection, _resolve_and_pin_host
from app.validation import normalize_public_http_url
from _testkit import block_private_targets



class TestSSRFProtection:
    def test_check_ssrf_protection_blocks_private_targets(self, monkeypatch):
        block_private_targets(monkeypatch)
        for target in ["http://localhost:8080/", "http://127.0.0.1:8080/", "http://192.168.1.1:8080/"]:
            with pytest.raises(ValueError):
                _check_ssrf_protection(target)

    def test_check_ssrf_protection_allows_public_ip(self, monkeypatch):
        block_private_targets(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]):
            # Should not raise for a public IP.
            _check_ssrf_protection("http://8.8.8.8:8080/")

    def test_resolve_and_pin_host_pins_first_public_ip(self, monkeypatch):
        block_private_targets(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 80))]):
            hostname, ip = _resolve_and_pin_host("http://example.com:8080/")
        assert hostname == "example.com"
        assert ip == "1.2.3.4"

    def test_normalize_public_http_url_blocks_private(self, monkeypatch):
        block_private_targets(monkeypatch)
        for target in ["http://localhost:8080/", "http://127.0.0.1:8080/", "http://10.0.0.1:8080/"]:
            with pytest.raises(ValueError, match="private"):
                normalize_public_http_url(target)

    def test_hostname_resolved_only_once(self, monkeypatch):
        """The SSRF check must resolve the hostname exactly once."""
        block_private_targets(monkeypatch)
        calls = {"n": 0}

        def mock_getaddrinfo(*args, **kwargs):
            calls["n"] += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]

        with patch("socket.getaddrinfo", side_effect=mock_getaddrinfo):
            _check_ssrf_protection("http://example.com:8080/")
        assert calls["n"] == 1
