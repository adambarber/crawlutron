"""HTTPS DNS-rebinding TOCTOU SSRF in crawlutron/transport/pinned_transport.

HTTPS kept the hostname (for SNI) and re-resolved at connect with no peer-IP
verification; these tests pin it and assert a rebind is fail-closed.
"""

from __future__ import annotations

import socket

import httpcore
import httpx
import pytest

from crawlutron.runtime import process_state
from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
from tests._assertions import block_private_targets
from tests._policy_factories import canonical

_PUBLIC_IP = "1.2.3.4"
_PRIVATE_IP = "169.254.169.254"


class _RecordingBackend(httpcore.AsyncNetworkBackend):
    """A real httpcore network backend that records every TCP connection it is asked
    to dial and serves a canned 200 instead of opening a socket."""

    def __init__(self):
        self.connects: list[tuple[str, int, str]] = []
        self.sni_hostnames: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        # These trailing params are part of httpcore's interface (passed by keyword);
        # this recording backend only needs the connect target.
        _ = (timeout, local_address, socket_options)
        addrinfo = socket.getaddrinfo(host, port)
        ip = str(addrinfo[0][4][0])
        self.connects.append((host, port, ip))
        return _StubStream(self)

    async def connect_unix_socket(self, *args, **kwargs):
        _ = (args, kwargs)
        raise NotImplementedError

    async def sleep(self, seconds):
        _ = seconds
        return None


class _StubStream(httpcore.AsyncNetworkStream):
    _RESPONSE = b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\ncontent-type: text/html\r\n\r\n<ok>"

    def __init__(self, backend):
        self._backend = backend

    async def read(self, max_bytes, timeout=None):
        _ = (max_bytes, timeout)
        return self._RESPONSE

    async def write(self, buffer, timeout=None):
        _ = (buffer, timeout)
        return None

    async def aclose(self):
        return None

    async def start_tls(self, ssl_context, server_hostname=None, timeout=None):
        _ = (ssl_context, timeout)
        self._backend.sni_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info):
        _ = info
        return None


def _recording_httpx_client(backend):
    """A real httpx client whose transport's httpcore pool dials through ``backend``."""
    pool = httpcore.AsyncConnectionPool(network_backend=backend)
    transport = httpx.AsyncHTTPTransport(verify=False)
    transport._pool = pool
    return httpx.AsyncClient(transport=transport)


def _install_client(monkeypatch, backend):
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: _recording_httpx_client(backend))


def _two_phase_resolver(monkeypatch):
    """Public IP on the first resolution, private/loopback on every later one.

    IP literals resolve to themselves (a pinned connect target stays pinned); only a
    hostname that rebinds sees the second, private answer.
    """

    def _is_ip_literal(host: str) -> bool:
        return ":" not in host and host.replace(".", "").isdigit()

    state = {"resolves": 0}

    def fake(host, *args, **kwargs):
        _ = (args, kwargs)
        if _is_ip_literal(host):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 80))]
        state["resolves"] += 1
        ip = _PUBLIC_IP if state["resolves"] == 1 else _PRIVATE_IP
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return state


def _stable_public_resolver(monkeypatch):
    """Every hostname resolution answers a public IP -- the honest (non-attacker) case."""

    def fake(host, *args, **kwargs):
        _ = (args, kwargs)
        if ":" not in host and host.replace(".", "").isdigit():
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, 80))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PUBLIC_IP, 80))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class TestHttpsDnsRebindingFetchIsBlocked:
    async def test_rebound_private_target_is_never_connected(self, monkeypatch):
        """A two-phase resolver (public at pin time, private after) must NEVER reach
        the private address: the pin connects only to the IP it just validated (#383).

        The private second-phase answer is never used -- it must appear in no connection.
        """
        block_private_targets(monkeypatch)
        process_state.reset_all_process_state()
        backend = _RecordingBackend()
        _install_client(monkeypatch, backend)
        _two_phase_resolver(monkeypatch)

        # The real production path: the real fetcher over the real transport.
        result = await default_http_fetcher(
            canonical("https://evil.example/ws"),
            {},
            30,
        )

        assert result.status_code == 200
        for _host, _port, ip in backend.connects:
            assert ip == _PUBLIC_IP, f"connected to a non-public IP {ip}: {backend.connects}"
        assert all(ip != _PRIVATE_IP for _h, _p, ip in backend.connects)

    async def test_private_answer_at_pin_time_is_fail_closed(self, monkeypatch):
        """The fail-closed discipline lives at the pin: a resolver whose answer AT
        PIN TIME is private/loopback is refused before any connection (the SSRF
        guarantee the double-resolve used to surface one hop later)."""
        block_private_targets(monkeypatch)
        process_state.reset_all_process_state()
        backend = _RecordingBackend()
        _install_client(monkeypatch, backend)

        def private_resolver(host, *args, **kwargs):
            _ = (args, kwargs)
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_PRIVATE_IP, 443))]

        monkeypatch.setattr(socket, "getaddrinfo", private_resolver)

        # The pin and check_ssrf_protection now share one verdict, so the
        # wording matches its sibling's "private/loopback" refusal (#525 SEC-07).
        with pytest.raises(ValueError, match="private/loopback"):
            await default_http_fetcher(
                canonical("https://evil.example/ws"),
                {},
                30,
            )
        assert backend.connects == [], f"the private-target request connected: {backend.connects}"

    async def test_plain_http_rebind_connects_only_to_the_validated_ip(self, monkeypatch):
        """Plain HTTP pins to a freshly validated IP, so a two-phase rebind cannot
        move the connection: every connect is the IP the pin itself validated."""
        block_private_targets(monkeypatch)
        process_state.reset_all_process_state()
        backend = _RecordingBackend()
        _install_client(monkeypatch, backend)
        _two_phase_resolver(monkeypatch)

        result = await default_http_fetcher(
            canonical("http://evil.example/ws"),
            {},
            30,
        )

        assert result.status_code == 200
        assert backend.connects, "the fetch never connected"
        assert all(ip == _PUBLIC_IP for _h, _p, ip in backend.connects), backend.connects


class TestHttpsSafeTargetFetchesWithSniPreserved:
    async def test_stable_public_target_fetches_over_the_pinned_ip_with_sni(self, monkeypatch):
        """A legitimate HTTPS target (no rebind) still fetches, connects to the validated
        public IP, and preserves the original hostname for TLS SNI -- the pin must not
        block legitimate traffic or break real-site TLS routing."""
        block_private_targets(monkeypatch)
        process_state.reset_all_process_state()
        backend = _RecordingBackend()
        _install_client(monkeypatch, backend)
        _stable_public_resolver(monkeypatch)

        result = await default_http_fetcher(
            canonical("https://example.org/secure"),
            {},
            30,
        )

        assert result.status_code == 200
        # The connect target is the validated public IP, not the hostname (no re-resolve).
        assert backend.connects == [("1.2.3.4", 443, "1.2.3.4")], backend.connects
        # TLS SNI carries the ORIGINAL hostname, so the origin still routes/attests correctly.
        assert backend.sni_hostnames == ["example.org"], backend.sni_hostnames
