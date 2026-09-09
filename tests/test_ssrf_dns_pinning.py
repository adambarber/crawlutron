"""TDD tests for SSRF DNS-rebinding (TOCTOU) mitigation via IP pinning.

These tests verify that ``_resolve_and_pin_host`` resolves the hostname exactly
once and returns the pinned IP used for the connection, and that private
targets are rejected. They use ``monkeypatch`` so the global environment is
restored automatically and never poisons other tests.
"""
import socket
from unittest.mock import patch

import pytest
from _testkit import assert_dns_pinning_uses_first_resolution, block_private_targets

from app.crawl.strategies.http import _check_ssrf_protection, _resolve_and_pin_host


class TestSSrfDnsPinning:
    def test_resolves_once_and_pins_ip(self, monkeypatch):
        block_private_targets(monkeypatch)
        calls = []

        def fake_getaddrinfo(host, *a, **k):
            calls.append(host)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            hostname, ip = _resolve_and_pin_host("http://example.com/")
        assert hostname == "example.com"
        assert ip == "93.184.216.34"
        assert calls == ["example.com"], "hostname should be resolved exactly once"

    def test_rebinding_second_resolution_returns_private_but_pinning_uses_first(self, monkeypatch):
        assert_dns_pinning_uses_first_resolution(monkeypatch, "127.0.0.1")

    def test_private_resolution_raises(self, monkeypatch):
        block_private_targets(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            with pytest.raises(ValueError):
                _resolve_and_pin_host("http://example.com/")

    def test_localhost_blocked_by_default(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            _check_ssrf_protection("http://localhost:8080/")
