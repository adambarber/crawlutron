"""TDD tests for CRITICAL #3: webhook SSRF (hostname not resolved).

The webhook validator must resolve DNS hostnames and reject any that map to a
private/loopback address, and the delivery POST must connect to a pinned IP
(DNS-rebinding safe) while preserving the original Host header.
"""
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from app.main import _validate_webhook_url, _deliver_webhook


def _block_private(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")


class TestWebhookUrlValidation:
    def test_blocks_private_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a private IP must be rejected."""
        _block_private(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            with pytest.raises(ValueError):
                _validate_webhook_url("http://evil.example.com/callback")

    def test_allows_public_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a public IP is accepted."""
        _block_private(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]):
            _validate_webhook_url("http://public.example.com/callback")

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            _validate_webhook_url("ftp://example.com/callback")

    def test_rejects_localhost(self, monkeypatch):
        _block_private(monkeypatch)
        with pytest.raises(ValueError):
            _validate_webhook_url("http://localhost:8080/callback")


class TestWebhookDeliveryPinning:
    async def test_delivery_posts_to_pinned_ip(self, monkeypatch):
        """The outbound POST must target the resolved/pinned IP, not the raw
        hostname, and preserve the original Host header (DNS-rebinding safe)."""
        _block_private(monkeypatch)
        captured = {}

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return MagicMock()

        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]),              patch("httpx.AsyncClient", _Client),              patch("app.main.crawl_request", new=AsyncMock(return_value={"results": []})):
            await _deliver_webhook("http://webhook.example.com/cb", {"results": []}, MagicMock())

        assert "8.8.8.8" in captured["url"], captured
        assert captured["headers"].get("Host") == "webhook.example.com", captured
