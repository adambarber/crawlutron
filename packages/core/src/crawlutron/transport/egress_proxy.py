"""Loopback CONNECT/HTTP forward proxy: one admit-or-refuse door, no MITM.

Shadow mode: the live egress paths are untouched. Callers dial through this
surface only when explicitly wired; the door itself owns every safety decision.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from crawlutron.runtime import process_state
from crawlutron.transport.pinned_dns import bounded_thread_resolve, resolve_and_pin_host
from safe_url import HostPolicy, NormalizedUrl, is_loopback_bind

__all__ = [
    "ProxyBindRefused",
    "ProxyServer",
    "ProxyTarget",
    "admit_proxy_target",
    "serve_proxy",
]

_LOGGER = logging.getLogger(__name__)
_REFUSAL_HEADER = "x-crawlutron-egress-refusal"
_Reader = asyncio.StreamReader
_Writer = asyncio.StreamWriter

_Dial = Callable[[str, int], Awaitable[tuple[_Reader, _Writer]]]
_REFUSAL_LINE = _REFUSAL_HEADER.lower().encode("ascii") + b":"
_DRAIN_TIMEOUT = 30.0
_MAX_HEAD = 65536

_LIVE_SERVERS: set[ProxyServer] = set()


class ProxyBindRefused(ValueError):
    """A non-loopback listen address was refused: egress stays on this machine."""


@dataclass(frozen=True, slots=True)
class ProxyTarget:
    """An admitted destination: logical host for identity, pinned IP for the dial."""

    url: NormalizedUrl
    pinned_ip: str
    port: int

    @property
    def logical_host(self) -> str:
        """The hostname TLS and Host must attest, never the pinned IP."""
        return self.url.host

    @property
    def tls_hostname(self) -> str | None:
        """The SNI the client keeps through the tunnel; None for plain HTTP."""
        return self.url.tls_hostname


def admit_proxy_target(authority: str, *, policy: HostPolicy, scheme: str = "https") -> ProxyTarget:
    """The one admit-or-refuse door: safe_url verdict plus a fresh public pin."""
    url = NormalizedUrl(f"{scheme}://{authority}/", policy=policy)
    _, pinned = resolve_and_pin_host(url)
    port = url.port or (443 if scheme == "https" else 80)
    return ProxyTarget(url=url, pinned_ip=pinned, port=port)


async def _admit(authority: str, *, policy: HostPolicy, scheme: str) -> ProxyTarget:
    """Off-loop admission: a resolver lookup never blocks the proxy event loop."""
    return await bounded_thread_resolve(
        lambda: admit_proxy_target(authority, policy=policy, scheme=scheme), timeout_seconds=None
    )


async def _refuse(writer: _Writer, status: int, reason: str, failure: str | None = None) -> None:
    """Answer with a fixed reason only; the target and its headers never appear."""
    marker = f"{_REFUSAL_HEADER}: {failure}\r\n" if failure else ""
    body = f"HTTP/1.1 {status} {reason}\r\n{marker}Content-Length: 0\r\nConnection: close\r\n\r\n"
    writer.write(body.encode("ascii"))
    await writer.drain()
    writer.close()


def _strip_refusal_header(head: bytes) -> bytes:
    """Remove the private marker from an origin's HTTP response header block."""
    kept = [line for line in head.split(b"\r\n") if not line.lower().startswith(_REFUSAL_LINE)]
    return b"\r\n".join(kept)


class _OriginHeadOverflow(Exception):
    """An origin response head crossed the cap without a header terminator."""


def _clean_origin_chunk(pending: bytes, data: bytes) -> tuple[bytes, bytes | None]:
    """Strip the marker once the header block is whole; buffer an incomplete one."""
    buffered = pending + data
    end = buffered.find(b"\r\n\r\n")
    if end < 0:
        if len(buffered) >= _MAX_HEAD:
            raise _OriginHeadOverflow
        return (b"", buffered)
    return _strip_refusal_header(buffered[: end + 4]) + buffered[end + 4 :], None


async def _client_to_origin(reader: _Reader, upstream_writer: _Writer) -> None:
    """Forward request bytes; a client write-EOF half-closes the origin."""
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            upstream_writer.write(data)
            await upstream_writer.drain()
    finally:
        # Client write-EOF means no more request bytes: half-close the origin's
        # write side. The client-facing writer stays open -- it may still read.
        if upstream_writer.can_write_eof():
            upstream_writer.write_eof()
        else:
            upstream_writer.close()


async def _origin_to_client(
    upstream_reader: _Reader, writer: _Writer, upstream_writer: _Writer, *, strip_refusal: bool
) -> None:
    """Forward origin bytes; a stalled client write releases the origin."""
    pending: bytes | None = b"" if strip_refusal else None
    try:
        while True:
            data = await upstream_reader.read(65536)
            if not data:
                break
            if pending is not None:
                try:
                    data, pending = _clean_origin_chunk(pending, data)
                except _OriginHeadOverflow:
                    await _refuse(writer, 502, "Bad-Gateway")
                    pending = b""
                    if upstream_writer.transport is not None:
                        upstream_writer.transport.abort()
                    return
                if pending is not None and not data:
                    continue
            try:
                writer.write(data)
                await asyncio.wait_for(writer.drain(), _DRAIN_TIMEOUT)
            except (TimeoutError, OSError):
                # A client that stopped reading must not leave the origin blocked
                # in send; the bounded drain is the release valve.
                if upstream_writer.transport is not None:
                    upstream_writer.transport.abort()
                raise
    finally:
        if pending:
            writer.write(pending)
        writer.close()
        upstream_writer.close()


async def _relay(
    reader: _Reader,
    writer: _Writer,
    upstream_reader: _Reader,
    upstream_writer: _Writer,
    *,
    strip_refusal: bool = False,
) -> None:
    """Copy bytes both ways; HTTP forward drops the origin's private marker."""
    await asyncio.gather(
        _client_to_origin(reader, upstream_writer),
        _origin_to_client(upstream_reader, writer, upstream_writer, strip_refusal=strip_refusal),
        return_exceptions=True,
    )


def _with_logical_host(raw: bytes, host: str, port: int, scheme: str) -> bytes:
    """Rewrite only the Host header; every other caller header rides verbatim."""
    default = 80 if scheme == "http" else 443
    value = host if port == default else f"{host}:{port}"
    lines = [line for line in raw.split(b"\r\n") if line]
    kept = [
        f"Host: {value}\r\n".encode("ascii") if line.lower().startswith(b"host:") else line + b"\r\n"
        for line in lines
    ]
    if not any(line.lower().startswith(b"host:") for line in lines):
        kept.insert(0, f"Host: {value}\r\n".encode("ascii"))
    return b"".join([*kept, b"\r\n"])


async def _admit_and_dial(
    authority: str, writer: _Writer, policy: HostPolicy, dial: _Dial, scheme: str, mode: str
) -> tuple[ProxyTarget, _Reader, _Writer] | None:
    """The one admit-then-dial path; both request shapes refuse identically."""
    try:
        plan = await _admit(authority, policy=policy, scheme=scheme)
    except (ValueError, OSError, TimeoutError):
        _LOGGER.warning("egress proxy refused a %s target", mode)
        await _refuse(writer, 403, "Forbidden")
        return None
    upstream_reader, upstream_writer = await _dial(plan, writer, dial)
    if upstream_reader is None or upstream_writer is None:
        return None
    return plan, upstream_reader, upstream_writer


async def _forward(
    reader: _Reader,
    writer: _Writer,
    method: str,
    target: str,
    headers: bytes,
    policy: HostPolicy,
    dial: _Dial,
) -> None:
    """Absolute-form HTTP hop: admit the authority, then relay with Host logical."""
    parts = urlsplit(target)
    if parts.scheme not in {"http", "https"} or not parts.netloc:
        await _refuse(writer, 400, "Bad-Request")
        return
    admitted = await _admit_and_dial(parts.netloc, writer, policy, dial, parts.scheme, "HTTP")
    if admitted is None:
        return
    plan, upstream_reader, upstream_writer = admitted
    origin_path = parts.path or "/"
    if parts.query:
        origin_path = f"{origin_path}?{parts.query}"
    request_line = f"{method} {origin_path} HTTP/1.1\r\n".encode("ascii")
    rewritten = _with_logical_host(headers, plan.logical_host, plan.port, plan.url.scheme)
    upstream_writer.write(request_line + rewritten)
    await upstream_writer.drain()
    await _relay(reader, writer, upstream_reader, upstream_writer, strip_refusal=True)


async def _tunnel(reader: _Reader, writer: _Writer, target: str, policy: HostPolicy, dial: _Dial) -> None:
    """CONNECT hop: admit the authority, then pipe raw TLS bytes untouched."""
    admitted = await _admit_and_dial(target, writer, policy, dial, "https", "CONNECT")
    if admitted is None:
        return
    _, upstream_reader, upstream_writer = admitted
    writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
    await writer.drain()
    await _relay(reader, writer, upstream_reader, upstream_writer)


async def _dial(plan: ProxyTarget, writer: _Writer, dial: _Dial) -> tuple[_Reader | None, _Writer | None]:
    """Dial the pinned IP; a dial fault becomes a response, never a traceback."""
    try:
        return await dial(plan.pinned_ip, plan.port)
    except OSError:
        _LOGGER.warning("egress proxy could not reach the pinned target")
        await _refuse(writer, 502, "Bad-Gateway", failure="dial-failed")
        return None, None


async def _read_headers(reader: _Reader) -> bytes | None:
    """Read one request's header block, capped so a hostile client cannot stall."""
    try:
        return await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 30.0)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ValueError):
        return None


def _make_handler(policy: HostPolicy, dial: _Dial):
    """Build one connection handler bound to this server's policy and dial seam."""

    async def handle(reader: _Reader, writer: _Writer) -> None:
        block = await _read_headers(reader)
        if block is None:
            await _refuse(writer, 400, "Bad-Request")
            return
        first_line, _, rest = block.partition(b"\r\n")
        try:
            first = first_line.decode("ascii").strip()
        except UnicodeError:
            await _refuse(writer, 400, "Bad-Request")
            return
        parts = first.split()
        if len(parts) != 3:
            await _refuse(writer, 400, "Bad-Request")
            return
        method, target, _version = parts
        if method == "CONNECT":
            await _tunnel(reader, writer, target, policy, dial)
        else:
            await _forward(reader, writer, method, target, rest, policy, dial)

    return handle


class ProxyServer:
    """A running loopback proxy; ``aclose`` stops it and drops its registration."""

    __slots__ = ("_port", "_server", "_bind_host")

    def __init__(self, server: asyncio.AbstractServer, port: int, bind_host: str) -> None:
        self._server = server
        self._port = port
        self._bind_host = bind_host

    @property
    def port(self) -> int:
        """The ephemeral loopback port this proxy listens on."""
        return self._port

    @property
    def bind_host(self) -> str:
        """The host address this proxy is bound to."""
        return self._bind_host

    @property
    def is_live(self) -> bool:
        """Whether the listener is still accepting connections."""
        return self._server.is_serving()

    async def aclose(self) -> None:
        """Stop accepting and leave the lifecycle registry."""
        self._server.close()
        await self._server.wait_closed()
        _LIVE_SERVERS.discard(self)


async def serve_proxy(
    *,
    policy: HostPolicy,
    host: str = "127.0.0.1",
    port: int = 0,
    dial: _Dial | None = None,
    sidecar_bind: ipaddress.IPv4Address | None = None,
) -> ProxyServer:
    """Start a CONNECT/HTTP proxy with an injectable dial seam.
    By default binds loopback-only; pass ``sidecar_bind=IPv4Address(socket.INADDR_ANY)``
    for dual-homed sidecar (only unspecified IPv4 address admitted).
    """
    # Validate sidecar_bind and host combination
    if sidecar_bind is not None:
        # Only the unspecified address (INADDR_ANY / 0.0.0.0) is admitted
        if sidecar_bind != ipaddress.IPv4Address(socket.INADDR_ANY):
            raise ProxyBindRefused("sidecar_bind admits only the unspecified IPv4 address (0.0.0.0)")
        # Conflicting host argument is refused
        if host != "127.0.0.1":
            raise ProxyBindRefused("host and sidecar_bind cannot both specify a non-loopback bind")
        bind_host = str(sidecar_bind)  # "0.0.0.0"
    else:
        # Default: loopback-only
        if not is_loopback_bind(host):
            raise ProxyBindRefused("egress proxy binds loopback only")
        bind_host = host

    upstream = dial if dial is not None else _open_target
    server = await asyncio.start_server(_make_handler(policy, upstream), bind_host, port)
    actual_port = server.sockets[0].getsockname()[1]
    wrapped = ProxyServer(server, actual_port, bind_host)
    _LIVE_SERVERS.add(wrapped)
    return wrapped


async def _open_target(host: str, port: int) -> tuple[_Reader, _Writer]:
    """The production dial: a raw TCP connection to the pinned IP."""
    return await asyncio.open_connection(host, port)


def _reset() -> None:
    """Stop every live proxy so a reset chain cannot leak listeners."""
    for server in tuple(_LIVE_SERVERS):
        server._server.close()
    _LIVE_SERVERS.clear()


def _has() -> bool:
    """Whether any proxy listener is still registered."""
    return bool(_LIVE_SERVERS)


process_state.register(__name__, _reset, _has)
