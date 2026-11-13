"""A recording httpcore backend for transport tests.

Records every TCP dial and TLS SNI handshake over the real connection pool so
tests can assert session/connection behavior without opening a socket.
"""

from __future__ import annotations

import socket

import httpcore
import httpx


class RecordingBackend(httpcore.AsyncNetworkBackend):
    """A real httpcore backend that records each connection it is asked to dial.

    ``connects`` carries ``(host, port, ip)`` (the ip resolved at connect), and
    ``sni_hostnames`` carries the TLS server name of every handshake.
    """

    def __init__(self) -> None:
        self.connects: list[tuple[str, int, str]] = []
        self.sni_hostnames: list[str] = []

    async def connect_tcp(self, host, port, timeout=None, local_address=None, socket_options=None):
        _ = (timeout, local_address, socket_options)
        ip = _resolve_connect_ip(host, port)
        self.connects.append((host, port, ip))
        return StubStream(self)

    async def connect_unix_socket(self, *args, **kwargs):
        _ = (args, kwargs)
        raise NotImplementedError

    async def sleep(self, seconds):
        _ = seconds
        return None


def _resolve_connect_ip(host: str, port: int) -> str:
    """The IP the backend claims to dial, so ``connects`` can be asserted against
    the validated pin (an IP literal resolves to itself)."""
    addrinfo = socket.getaddrinfo(host, port)
    return str(addrinfo[0][4][0])


class StubStream(httpcore.AsyncNetworkStream):
    """A stream serving a canned 200 and recording each TLS SNI handshake."""

    _RESPONSE = b"HTTP/1.1 200 OK\r\ncontent-length: 4\r\ncontent-type: text/html\r\n\r\n<ok>"

    def __init__(self, backend: RecordingBackend) -> None:
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


def recording_httpx_client(backend: RecordingBackend) -> httpx.AsyncClient:
    """A real httpx client whose transport's httpcore pool dials through ``backend``."""
    pool = httpcore.AsyncConnectionPool(network_backend=backend)
    transport = httpx.AsyncHTTPTransport(verify=False)
    transport._pool = pool
    return httpx.AsyncClient(transport=transport)


def is_ip_literal(host: str) -> bool:
    """Whether ``host`` is a dotted IPv4 literal (resolves to itself)."""
    return ":" not in host and host.replace(".", "").isdigit()


def install_public_resolver(monkeypatch, ip: str, port: int) -> None:
    """Script the resolver so every hostname resolves to ``ip:port`` and every IP
    literal to itself -- a shared-CDN posture for cross-host pooling tests."""

    def fake(host, *args, **kwargs):
        _ = (args, kwargs)
        if is_ip_literal(host):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (host, port))]
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


__all__ = [
    "RecordingBackend",
    "StubStream",
    "install_public_resolver",
    "is_ip_literal",
    "recording_httpx_client",
]
