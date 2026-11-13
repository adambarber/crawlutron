"""HTTPS DNS-rebinding TOCTOU SSRF in crawlutron/transport/pinned_transport.

HTTPS kept the hostname (for SNI) and re-resolved at connect with no peer-IP
verification; these tests pin it and assert a rebind is fail-closed.
"""

from __future__ import annotations

import socket

import pytest

from crawlutron.runtime import process_state
from crawlutron.transport import http_client_pool as pool_mod
from crawlutron.transport.fetchers import default_http_fetcher
from tests._assertions import block_private_targets
from tests._http_recording_backend import (
    RecordingBackend,
    install_public_resolver,
    is_ip_literal,
    recording_httpx_client,
)
from tests._policy_factories import canonical

_PUBLIC_IP = "1.2.3.4"
_PRIVATE_IP = "169.254.169.254"


def _install_client(monkeypatch, backend):
    monkeypatch.setattr(pool_mod, "get_async_http_client", lambda: recording_httpx_client(backend))


def _two_phase_resolver(monkeypatch):
    """Public IP on the first resolution, private/loopback on every later one.

    IP literals resolve to themselves (a pinned connect target stays pinned); only a
    hostname that rebinds sees the second, private answer.
    """

    def _is_ip_literal(host: str) -> bool:
        return is_ip_literal(host)

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
    install_public_resolver(monkeypatch, _PUBLIC_IP, 80)


class TestHttpsDnsRebindingFetchIsBlocked:
    async def test_rebound_private_target_is_never_connected(self, monkeypatch):
        """A two-phase resolver (public at pin time, private after) must NEVER reach
        the private address: the pin connects only to the IP it just validated (#383).

        The private second-phase answer is never used -- it must appear in no connection.
        """
        block_private_targets(monkeypatch)
        process_state.reset_all_process_state()
        backend = RecordingBackend()
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
        backend = RecordingBackend()
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
        backend = RecordingBackend()
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
        backend = RecordingBackend()
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
