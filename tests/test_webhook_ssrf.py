"""TDD tests for CRITICAL #3: webhook SSRF (hostname not resolved).

The webhook validator must resolve DNS hostnames and reject any that map to a
private/loopback address, and the delivery POST must connect to a pinned IP
(DNS-rebinding safe) while preserving the original Host header.
"""

import socket
from contextlib import contextmanager
from unittest.mock import patch

import httpx
import pytest
from _assertions import block_private_targets

from app.api.webhooks import (
    post_webhook as _post_webhook,
)
from app.api.webhooks import (
    validate_webhook_url as _validate_webhook_url,
)
from app.validation import HostPolicy


def _fake_httpx_client(capture=None, body=b""):
    """httpx.AsyncClient double used by the delivery tests.

    Records ``stream()`` args into ``capture`` and yields ``body`` as the
    response; the delivery path aliases httpx at import time, so the same
    double observes the call ``post_webhook`` makes into
    ``deliver_pinned_webhook``.
    """

    class _Response:
        def __init__(self):
            self.status_code = 200

        async def aiter_bytes(self):
            yield body

    class _StreamCtx:
        async def __aenter__(self):
            return _Response()

        async def __aexit__(self, *exc):
            return False

    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return False  # httpx's __aexit__ never suppresses

        def stream(self, method, url, content=None, headers=None):
            if capture is not None:
                capture["url"] = url
                capture["headers"] = headers
            return _StreamCtx()

    return _Client


def _pinned_webhook_client(capture=None, body=b""):
    """Patch httpx.AsyncClient (recording/capped double) + DNS pin for delivery tests."""
    original = httpx.AsyncClient
    httpx.AsyncClient = _fake_httpx_client(capture=capture, body=body)

    @contextmanager
    def _patched():
        try:
            with patch(
                "socket.getaddrinfo",
                return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))],
            ):
                yield
        finally:
            httpx.AsyncClient = original

    return _patched()


class TestWebhookUrlValidation:
    def test_blocks_private_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a private IP must be rejected."""
        block_private_targets(monkeypatch)
        with patch(
            "socket.getaddrinfo",
            return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))],
        ):
            with pytest.raises(ValueError):
                _validate_webhook_url(
                    "http://evil.example.com/callback",
                    host_policy=HostPolicy.unrestricted(),
                )

    def test_allows_public_dns_hostname(self, monkeypatch):
        """A DNS name that resolves to a public IP is accepted."""
        block_private_targets(monkeypatch)
        with patch(
            "socket.getaddrinfo", return_value=[(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 80))]
        ):
            _validate_webhook_url(
                "http://public.example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    def test_rejects_non_http_scheme(self):
        with pytest.raises(ValueError):
            _validate_webhook_url(
                "ftp://example.com/callback",
                host_policy=HostPolicy.unrestricted(),
            )

    def test_rejects_localhost(self, monkeypatch):
        block_private_targets(monkeypatch)
        with pytest.raises(ValueError):
            _validate_webhook_url(
                "http://localhost:8080/callback",
                host_policy=HostPolicy.unrestricted(),
            )


class TestWebhookDeliveryPinning:
    async def test_delivery_posts_to_pinned_ip(self, monkeypatch):
        """The outbound POST must target the resolved/pinned IP, not the raw
        hostname, and preserve the original Host header (DNS-rebinding safe).

        Driven through the production funnel ``post_webhook`` -- not a deleted
        island entry point -- so the guard protects the path real crawls take.
        """
        block_private_targets(monkeypatch)
        captured = {}
        with _pinned_webhook_client(capture=captured):
            await _post_webhook(
                "http://webhook.example.com/cb",
                {"results": []},
                host_policy=HostPolicy.unrestricted(),
            )

        assert "8.8.8.8" in captured["url"], captured
        assert captured["headers"].get("Host") == "webhook.example.com", captured

    async def test_https_delivery_uses_validated_hostname_not_ip(self, monkeypatch):
        """L3: rewriting an https URL to the pinned IP breaks TLS (certs
        never carry IP SANs), so HTTPS webhooks were effectively dead.
        HTTPS delivery POSTs to the validated hostname; HTTP keeps IP pinning."""
        from app.api.webhooks import deliver_pinned_webhook

        block_private_targets(monkeypatch)
        captured = {}
        with _pinned_webhook_client(capture=captured):
            await deliver_pinned_webhook(
                "https://webhook.example.com/cb",
                b"{}",
                host_policy=HostPolicy.unrestricted(),
            )

        assert captured["url"] == "https://webhook.example.com/cb", captured
        assert "8.8.8.8" not in captured["url"], captured

    async def test_delivery_response_body_is_capped(self, monkeypatch, caplog):
        """M3: the webhook RESPONSE is attacker-controlled and must never be
        buffered unboundedly. The delivery must consume it through the
        bounded_io cap and survive an oversized body without raising."""
        import logging

        from app.api.webhooks import _max_webhook_size, deliver_pinned_webhook

        block_private_targets(monkeypatch)
        big_body = b"x" * (_max_webhook_size() + 1)
        with _pinned_webhook_client(body=big_body):
            with caplog.at_level(logging.WARNING, logger="app.api.webhooks"):
                await deliver_pinned_webhook(
                    "http://webhook.example.com/cb",
                    b"{}",
                    host_policy=HostPolicy.unrestricted(),
                )

        assert any("webhook_response_too_large" in r.message for r in caplog.records), caplog.text


def test_webhook_size_cap_honors_a_live_env_override(monkeypatch):
    """P2-F: the cap is read 2-3 times per delivery and used to be re-read and
    re-parsed from ``os.environ`` on every call. Caching it on the raw env value
    parses once per distinct value while an override -- operator env, or a test
    -- is still picked up on the very next call."""
    from app.api import webhooks

    monkeypatch.setenv("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "1234")
    assert webhooks._max_webhook_size() == 1234, "the override was not honored"

    # A later change to the same env var is reflected without a reload.
    monkeypatch.setenv("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "4321")
    assert webhooks._max_webhook_size() == 4321, "a cached value shadowed the new override"

    # Removing the override restores the default, not a stale cached value.
    monkeypatch.delenv("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE")
    assert webhooks._max_webhook_size() == 10_485_760


def test_webhook_size_cap_keeps_the_unit_floor(monkeypatch):
    """The ``lo=1`` floor survives the caching path: a zero/negative cap would
    cap every delivery at nothing and silently drop all webhooks."""
    from app.api import webhooks

    monkeypatch.setenv("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "0")
    assert webhooks._max_webhook_size() == 1

    monkeypatch.setenv("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "not-an-int")
    assert webhooks._max_webhook_size() == 10_485_760, "a garbage value must fall back, not raise"
