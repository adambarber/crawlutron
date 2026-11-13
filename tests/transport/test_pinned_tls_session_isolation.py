"""Cross-logical-host TLS session isolation on pinned HTTPS hops.

Two hosts on one pinned IP share one pool key; the fix closes the connection so
no logical host ever reuses another's TLS session.
"""

from __future__ import annotations

import socket

from crawlutron.runtime import process_state
from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.pinned_transport import build_pinned_request, cffi_resolve_entry
from tests._assertions import block_private_targets
from tests._http_recording_backend import (
    RecordingBackend,
    install_public_resolver,
    recording_httpx_client,
)
from tests._policy_factories import canonical

_PUBLIC_IP = "1.2.3.4"


def _install_client(monkeypatch, backend):
    # One shared client: production returns the SAME pooled client each fetch, so
    # the pool is what the cross-host isolation fix must break.
    client = recording_httpx_client(backend)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)


def _same_ip_resolver(monkeypatch):
    """Every logical hostname resolves to the SAME public IP:443 -- a shared CDN."""
    block_private_targets(monkeypatch)
    install_public_resolver(monkeypatch, _PUBLIC_IP, 443)


class TestPinnedHttpsHopsNeverShareATlsSession:
    """The core security pin: two logical hosts on one IP each get a fresh handshake."""

    async def test_two_logical_hosts_on_one_ip_get_distinct_connections(self, monkeypatch):
        """Fetching two different hosts that share a pinned IP:port must dial TWO
        separate connections and attest TWO distinct SNI names -- never reuse one
        pooled session (the first host's SNI/cert) for the second host."""
        process_state.reset_all_process_state()
        backend = RecordingBackend()
        _install_client(monkeypatch, backend)
        _same_ip_resolver(monkeypatch)

        await default_http_fetcher(canonical("https://cdn-alpha.example/a"), {}, 10)
        await default_http_fetcher(canonical("https://cdn-beta.example/b"), {}, 10)

        # Two distinct TCP dials: the pool must not serve either host off the other's connection.
        assert len(backend.connects) == 2, backend.connects
        # Every dial dials the pinned IP; the SNI carries each logical host.
        assert {host for host, _port, _ip in backend.connects} == {_PUBLIC_IP}
        assert backend.sni_hostnames == ["cdn-alpha.example", "cdn-beta.example"], backend.sni_hostnames


class TestSingleLogicalHostRepeats:
    """Direction-1 (keepalive-disable) trades reuse for isolation: one logical host
    fetched repeatedly still succeeds, each over its own fresh handshake."""

    async def test_repeated_fetch_of_one_host_succeeds_with_distinct_connections(self, monkeypatch):
        process_state.reset_all_process_state()
        backend = RecordingBackend()
        _install_client(monkeypatch, backend)
        _same_ip_resolver(monkeypatch)

        for _ in range(2):
            await default_http_fetcher(canonical("https://cdn-alpha.example/a"), {}, 10)

        # No reuse even for the same logical host: each fetch is a fresh connection,
        # and every handshake attests the SAME hostname.
        assert len(backend.connects) == 2, backend.connects
        assert backend.sni_hostnames == ["cdn-alpha.example", "cdn-alpha.example"], backend.sni_hostnames


class TestKeepaliveDisableIsScoped:
    """The fix must touch ONLY the vulnerable class: HTTPS hops carrying a logical
    hostname. Plain HTTP and IP-literal TLS keep full pooling (no header)."""

    def test_pinned_https_with_hostname_asks_for_connection_close(self, monkeypatch):
        _pinned_resolver(monkeypatch, ("93.184.216.34", 443))
        request = build_pinned_request(canonical("https://example.com/secure.pdf"), {})
        assert request.headers["Connection"] == "close"

    def test_plain_http_keeps_keepalive(self, monkeypatch):
        # HTTP hops dial the logical host directly: no same-origin collision, so
        # keepalive stays on (the fix must not touch non-pinned traffic).
        _pinned_resolver(monkeypatch, ("93.184.216.34", 80))
        request = build_pinned_request(canonical("http://example.com/a"), {})
        assert "Connection" not in request.headers

    def test_ip_literal_tls_keeps_keepalive(self, monkeypatch):
        # An IP literal attests to itself: no separate logical host is riding SNI,
        # so there is no cross-host collision to isolate from.
        _pinned_resolver(monkeypatch, ("93.184.216.34", 443))
        request = build_pinned_request(canonical("https://93.184.216.34/"), {})
        assert request.headers.get("Connection") != "close"

    def test_curl_cffi_resolve_table_is_unchanged(self, monkeypatch):
        """The curl_cffi path drives SNI via its own resolve table (host:port:ip),
        not the httpcore pool, so the header must not disturb its addressing."""
        _pinned_resolver(monkeypatch, ("93.184.216.34", 443))
        request = build_pinned_request(canonical("https://example.com/secure.pdf"), {})
        entry = cffi_resolve_entry(request)
        assert entry == ["example.com:443:93.184.216.34"]


def _pinned_resolver(monkeypatch, addrs):
    """Script the resolver to ``addrs`` with the private-target guard forced on."""
    block_private_targets(monkeypatch)
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda host, *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", addrs)],
    )
