"""HIGH fix: when ALLOW_PRIVATE_TARGETS=1 (local/dev mode), the SSRF
escape hatch must NOT hardcode the pinned IP to 127.0.0.1.

Returning "127.0.0.1" made every HTTP fetch and webhook POST to loopback
regardless of the requested host, breaking virtual-host routing and silently
misrouting traffic. The escape hatch should simply bypass resolution and use
the original hostname.
"""
import pytest

from app.crawl.strategies.http import _resolve_and_pin_host


def test_allow_private_returns_original_hostname_not_loopback(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = _resolve_and_pin_host("http://example.com/")
    assert hostname == "example.com"
    # Must connect to the requested host, not hardcoded loopback.
    assert pinned == "example.com"
    assert pinned != "127.0.0.1"


def test_allow_private_preserves_host_for_webhook_style_urls(monkeypatch):
    # A non-loopback host under local dev mode stays as-is (so webhooks and
    # fetches to the real hostname are not misrouted to 127.0.0.1).
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "1")
    hostname, pinned = _resolve_and_pin_host("https://hooks.svc.local/path")
    assert hostname == "hooks.svc.local"
    assert pinned == "hooks.svc.local"
    assert pinned != "127.0.0.1"
