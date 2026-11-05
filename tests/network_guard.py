"""Network guard: tests must not reach real network."""

from __future__ import annotations

import socket

import pytest

_ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "", "0.0.0.0"})

_INSTALLED = False
_REAL_CONNECT = socket.socket.connect
_REAL_CREATE = socket.create_connection
_REAL_GAI = socket.getaddrinfo


def is_installed() -> bool:
    """Whether the guards are armed in this process (the bite tests' precondition)."""
    return _INSTALLED


def _loopback(host: object) -> bool:
    if not isinstance(host, str):
        return True
    return host in _ALLOWED_HOSTS or host.startswith("127.") or host.endswith(".local")


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


def _guarded_connect(self, address):
    host = address[0] if isinstance(address, tuple) and address else None
    if host is not None and not _loopback(host):
        _refuse("socket.connect", _dest(address))
    return _REAL_CONNECT(self, address)


def _guarded_create(address, *a, **kw):
    if address and not _loopback(address[0]):
        _refuse("socket.create_connection", _dest(address))
    return _REAL_CREATE(address, *a, **kw)


def _guarded_gai(host, *a, **kw):
    # IP literals are parsed, not resolved; a real DNS name is the violation.
    if isinstance(host, str) and not _loopback(host) and not _is_ip_literal(host):
        _refuse("socket.getaddrinfo", host)
    return _REAL_GAI(host, *a, **kw)


def pytest_configure(config: pytest.Config) -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    # Autouse plugin hooks armed before any test can dial out. The class-method
    # swap needs setattr (a typed assignment is refused statically).
    setattr(socket.socket, "connect", _guarded_connect)  # noqa: B010 - the seam is the subject
    socket.create_connection = _guarded_create
    socket.getaddrinfo = _guarded_gai
    _INSTALLED = True


def pytest_unconfigure(config: pytest.Config) -> None:
    global _INSTALLED
    if not _INSTALLED:
        return
    setattr(socket.socket, "connect", _REAL_CONNECT)  # noqa: B010 - the seam is the point
    socket.create_connection = _REAL_CREATE
    socket.getaddrinfo = _REAL_GAI
    _INSTALLED = False
