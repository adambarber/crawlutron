"""Cross-logical-host TLS identity on logical HTTPS hops.

The proxy is the sole dialer: the app carries the logical URL and the SNI the
client must attest, so two logical hosts never share a TLS identity.
"""

from __future__ import annotations

from crawlutron.runtime import process_state
from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
from crawlutron.transport.transport import FetchRequest
from tests._http_recording_backend import RecordingBackend, install_public_resolver, recording_httpx_client
from tests._policy_factories import canonical


def _install_client(monkeypatch, backend):
    client = recording_httpx_client(backend)
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: client)


class TestLogicalHttpsHopsNeverShareATlsIdentity:
    """Two logical hosts are carried to the proxy with distinct SNI names."""

    async def test_two_logical_hosts_get_distinct_handshakes(self, monkeypatch):
        process_state.reset_all_process_state()
        backend = RecordingBackend()
        _install_client(monkeypatch, backend)
        install_public_resolver(monkeypatch, "1.2.3.4", 443)

        await default_http_fetcher(canonical("https://cdn-alpha.example/a"), {}, 10)
        await default_http_fetcher(canonical("https://cdn-beta.example/b"), {}, 10)

        assert len(backend.connects) == 2, backend.connects
        assert {host for host, _port, _ip in backend.connects} == {
            "cdn-alpha.example",
            "cdn-beta.example",
        }
        assert backend.sni_hostnames == ["cdn-alpha.example", "cdn-beta.example"], backend.sni_hostnames


class TestSingleLogicalHostRepeats:
    """A logical host fetched repeatedly reuses the pooled connection."""

    async def test_repeated_fetch_of_one_host_succeeds(self, monkeypatch):
        process_state.reset_all_process_state()
        backend = RecordingBackend()
        _install_client(monkeypatch, backend)
        install_public_resolver(monkeypatch, "1.2.3.4", 443)

        for _ in range(2):
            await default_http_fetcher(canonical("https://cdn-alpha.example/a"), {}, 10)

        assert len(backend.connects) == 1, backend.connects
        assert backend.sni_hostnames == ["cdn-alpha.example"], backend.sni_hostnames


class TestTlsIdentityIsScopedToHostnameHttps:
    """Only an HTTPS hop with a hostname carries an SNI identity."""

    def test_https_hostname_carries_its_identity(self):
        request = FetchRequest(headers={}, url=canonical("https://example.com/secure.pdf"), timeout_seconds=1)
        assert request.tls_hostname == "example.com"

    def test_plain_http_carries_no_identity(self):
        request = FetchRequest(headers={}, url=canonical("http://example.com/a"), timeout_seconds=1)
        assert request.tls_hostname is None
