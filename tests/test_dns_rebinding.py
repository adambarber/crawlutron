"""Tests that the DNS-rebinding (TOCTOU) fix actually pins the IP.

Before the fix, the HTTP client re-resolved the hostname at connect time, so a
second DNS response could return a private IP and bypass SSRF protection. After
the fix, ``_resolve_and_pin_host`` resolves once and the fetcher connects to the
pinned IP with the original ``Host`` header preserved.
"""
import socket
from unittest.mock import AsyncMock, patch

import pytest

from app.crawl.strategies.http import _resolve_and_pin_host, _default_http_fetcher


def _block_private(monkeypatch):
    monkeypatch.setenv("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", "0")


class TestDNSRebindingFix:
    def test_resolve_and_pin_host_uses_first_resolution(self, monkeypatch):
        """Even if a second resolution would rebound to a private IP, the pinned
        IP (first resolution) is what the connection uses."""
        _block_private(monkeypatch)
        calls = []

        def fake_getaddrinfo(host, *a, **k):
            calls.append(host)
            if len(calls) == 1:
                return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 80))]
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("169.254.169.254", 80))]

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo):
            hostname, ip = _resolve_and_pin_host("http://example.com/")
        assert ip == "93.184.216.34"
        assert len(calls) == 1

    async def test_fetcher_connects_to_pinned_ip_with_original_host(self, monkeypatch):
        """The HTTP fetcher must connect to the pinned IP and forward the
        original Host header (so virtual-host routing is preserved)."""
        _block_private(monkeypatch)
        import urllib.parse as up
        captured = {}

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/html"}
            class _Url:
                def __str__(self):
                    return "http://1.2.3.4:80/"
            url = _Url()
            content = b"<html></html>"
            async def aread(self):
                return self.content
            async def aiter_raw(self):
                yield self.content

        class _Stream:
            def __init__(self, resp):
                self._resp = resp
            async def __aenter__(self):
                return self._resp
            async def __aexit__(self, *a):
                return False

        class _Client:
            def stream(self, method, url, **kwargs):
                captured["method"] = method
                captured["url"] = url
                captured["host_header"] = kwargs.get("headers", {}).get("Host")
                return _Stream(_Resp())

        def fake_getaddrinfo(host, *a, **k):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("1.2.3.4", 80))]

        with patch("socket.getaddrinfo", side_effect=fake_getaddrinfo),              patch("app.crawl.strategies.http.get_async_http_client", return_value=_Client()):
            result = await _default_http_fetcher("http://example.com/page", {}, 10, 50 * 1024 * 1024)

        assert captured["url"].startswith("http://1.2.3.4:80"), captured
        assert captured["host_header"] == "example.com", captured
        assert result.status_code == 200
