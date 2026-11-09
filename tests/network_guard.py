"""Network guard: tests must not reach real network."""

from __future__ import annotations

import socket
from unittest import mock

import pytest

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "", "0.0.0.0"})

_INSTALLED = False
_REAL_CONNECT = socket.socket.connect
_REAL_CONNECT_EX = socket.socket.connect_ex
_REAL_SENDTO = socket.socket.sendto
_REAL_CREATE = socket.create_connection
_REAL_GAI = socket.getaddrinfo
_REAL_GHB = socket.gethostbyname
_REAL_GHBX = socket.gethostbyname_ex


def is_installed() -> bool:
    """Whether the guards are armed in this process (the bite tests' precondition)."""
    return _INSTALLED


def _loopback(host: object) -> bool:
    if not isinstance(host, str):
        return True
    return host in _ALLOWED_HOSTS or host.startswith("127.")


def _is_ip_literal(host: str) -> bool:
    try:
        socket.inet_pton(socket.AF_INET, host)
        return True
    except OSError:
        pass
    try:
        socket.inet_pton(socket.AF_INET6, host.strip("[]"))
        return True
    except OSError:
        return False


def _dest(address: object) -> str:
    if isinstance(address, tuple):
        return str(address[0])
    return str(address)


def _refuse(where: str, destination: str) -> None:
    pytest.fail(
        f"hermeticity violation: {where} reached non-loopback destination "
        f"{destination!r} — tests use the MockTransport seam or a loopback server",
        pytrace=False,
    )


def _peer_of(address) -> str | None:
    host = address[0] if isinstance(address, tuple) and address else None
    return str(host) if host is not None else None


def _guarded_connect(self, address):
    host = _peer_of(address)
    if host is not None and not _loopback(host):
        _refuse("socket.connect", _dest(address))
    return _REAL_CONNECT(self, address)


def _guarded_connect_ex(self, address):
    host = _peer_of(address)
    if host is not None and not _loopback(host):
        _refuse("socket.connect_ex", _dest(address))
    return _REAL_CONNECT_EX(self, address)


def _guarded_sendto(self, data, address=None, *a, **kw):
    # UDP dials without connect: the destination arrives per-call and is never
    # seen by any connect guard.
    if address is not None:
        host = _peer_of(address)
        if host is not None and not _loopback(host):
            _refuse("socket.sendto", _dest(address))
    return _REAL_SENDTO(self, data, address, *a, **kw)


def _guarded_create(address, *a, **kw):
    if address and not _loopback(address[0]):
        _refuse("socket.create_connection", _dest(address))
    return _REAL_CREATE(address, *a, **kw)


def _name_resolves(host: object, where: str) -> bool:
    """True when host is a real DNS name (not loopback, not an IP literal)."""
    return isinstance(host, str) and not _loopback(host) and not _is_ip_literal(host)


def _guarded_gai(host, *a, **kw):
    # IP literals are parsed, not resolved; a real DNS name is the violation.
    if _name_resolves(host, "socket.getaddrinfo"):
        _refuse("socket.getaddrinfo", host)
    return _REAL_GAI(host, *a, **kw)


def _guarded_ghb(host, *a, **kw):
    if _name_resolves(host, "socket.gethostbyname"):
        _refuse("socket.gethostbyname", host)
    return _REAL_GHB(host, *a, **kw)


def _guarded_ghbx(host, *a, **kw):
    if _name_resolves(host, "socket.gethostbyname_ex"):
        _refuse("socket.gethostbyname_ex", host)
    return _REAL_GHBX(host, *a, **kw)


def pytest_configure(config: pytest.Config) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Autouse plugin hooks armed before any test can dial out: the class-method
    # swap goes through mock.patch.object, whose start/stop owns the restore below.
    mock.patch.object(socket.socket, "connect", _guarded_connect).start()
    mock.patch.object(socket.socket, "connect_ex", _guarded_connect_ex).start()
    mock.patch.object(socket.socket, "sendto", _guarded_sendto).start()
    socket.create_connection = _guarded_create
    socket.getaddrinfo = _guarded_gai
    socket.gethostbyname = _guarded_ghb
    socket.gethostbyname_ex = _guarded_ghbx
    _INSTALLED = True


def pytest_unconfigure(config: pytest.Config) -> None:
    global _INSTALLED
    if not _INSTALLED:
        return
    mock.patch.stopall()
    socket.create_connection = _REAL_CREATE
    socket.getaddrinfo = _REAL_GAI
    socket.gethostbyname = _REAL_GHB
    socket.gethostbyname_ex = _REAL_GHBX
    _INSTALLED = False
