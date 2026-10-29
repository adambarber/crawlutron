"""Impersonation failures must not silently degrade to plain httpx (#439)."""

import ssl

import pytest
from curl_cffi.requests.exceptions import SSLError as CffiSSLError

import crawlutron.transport.http_client_pool as http_mod
from crawlutron.transport.fetchers import default_http_fetcher
from tests._policy_factories import canonical


class _HandshakeFailingSession:
    """A cffi session whose TLS handshake always fails: the #439 wire shape."""

    def __init__(self):
        self.called = False

    async def get(self, *args, **kwargs):
        self.called = True
        raise CffiSSLError("handshake failure")


async def test_tls_handshake_failure_is_not_silently_served_by_httpx(monkeypatch):
    """HTTP_CFFI_IMPERSONATE against a TLS-refusing endpoint must not return a
    200 that pretends impersonation worked."""
    import socket

    cffi = _HandshakeFailingSession()
    pinned = ("93.184.216.34", 443)  # port differs from the #441 probe's
    fake_dns = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", pinned)]
    monkeypatch.setattr(http_mod, "get_cffi_session", lambda: cffi)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: fake_dns)
    url = canonical("https://example.com/")

    with pytest.raises(Exception, match="handshake"):
        await default_http_fetcher(url, {}, 10, 1024, impersonate=True)
    assert cffi.called, "the cffi transport was never reached"


async def test_cffi_ssl_error_classifies_as_tls_error():
    """The classification table owns the verdict: curl_cffi TLS failures are
    tls_error, so the ladder reports them instead of misreading the wire."""
    from crawlutron.transport.classification import classify_error_code

    assert classify_error_code(CffiSSLError("handshake failure")) == "tls_error"
    assert classify_error_code(ssl.SSLError("x")) == "tls_error"
