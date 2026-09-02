"""TDD tests for CRITICAL #3: webhook SSRF (hostname not resolved).

The webhook validator must resolve DNS hostnames and reject any that map to a
private/loopback address, and the delivery POST must connect to a pinned IP
(DNS-rebinding safe) while preserving the original Host header.
"""
import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx

from app.api import webhooks as _webhook_module
from app.api.webhooks import (
    set_webhook_dependencies,
    clear_webhook_dependencies,
    validate_webhook_url as _validate_webhook_url,
    deliver_webhook as _deliver_webhook,
)
from _testkit import block_private_targets


class _FakeUnifiedResponse:
    """Stub for ``UnifiedCrawlResponse`` that returns a JSON-serializable dict."""

    def __init__(self, **_kwargs):
        self.kwargs = _kwargs

    def model_dump(self):
        return {
            "request_id": self.kwargs.get("request_id", "test"),
            "results": self.kwargs.get("results", []),
        }



class TestWebhookUrlValidation:
    def test_blocks_private_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a private IP must be rejected."""
        block_private_targets(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]):
            with pytest.raises(ValueError):
                _validate_webhook_url("http://evil.example.com/callback")

    def test_allows_public_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a public IP is accepted."""
        block_private_targets(monkeypatch)
        with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]):
            _validate_webhook_url("http://public.example.com/callback")

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            _validate_webhook_url("ftp://example.com/callback")

    def test_rejects_localhost(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            _validate_webhook_url("http://localhost:8080/callback")

class TestWebhookDeliveryPinning:
    async def test_delivery_posts_to_pinned_ip(self, monkeypatch):
        """The outbound POST must target the resolved/pinned IP, not the raw
        hostname, and preserve the original Host header (DNS-rebinding safe)."""
        block_private_targets(monkeypatch)
        captured = {}

        class _MockResponse:
            def __init__(self):
                self.status_code = 200

        class _Client:
            def __init__(self, *args, **kwargs):
                # Store args to verify timeout etc.
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                # Properly clean up - return True to suppress any exceptions
                return True

            async def post(self, url, content=None, headers=None):
                captured["url"] = url
                captured["headers"] = headers
                return _MockResponse()

        # httpx.AsyncClient is imported lazily inside deliver_pinned_webhook, so
        # monkeypatching the module-level httpx module covers all entry points.
        import asyncio
        _original_async_client = httpx.AsyncClient
        httpx.AsyncClient = _Client
        try:
            with patch("socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]):
                # Initialize webhook module-level state with a fake crawl_request.
                set_webhook_dependencies(
                    webhook_semaphore=asyncio.Semaphore(1),
                    shutdown_event=asyncio.Event(),
                    crawl_request_fn=AsyncMock(return_value={"results": []}),
                    pool_getter=lambda: MagicMock(),
                    unified_response_cls=_FakeUnifiedResponse,
                    build_unified_result=lambda x: x,
                    get_metrics=lambda: MagicMock(observe_crawl=lambda **kw: None),
                )
                try:
                    await _deliver_webhook("http://webhook.example.com/cb", "crawl_test", {"results": []}, MagicMock())
                finally:
                    clear_webhook_dependencies()
        finally:
            httpx.AsyncClient = _original_async_client

        assert "8.8.8.8" in captured["url"], captured
        assert captured["headers"].get("Host") == "webhook.example.com", captured
