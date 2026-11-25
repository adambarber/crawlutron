"""Loopback egress proxy: admit-or-refuse door, CONNECT tunnel, HTTP forward."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from crawlutron.transport.egress_proxy import admit_proxy_target, serve_proxy
from tests._assertions import resolves_in_sequence
from tests._fetcher_factories import ParkedResolver
from tests._host_assert import host_of
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, host_policy_for

Dial = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]

PUBLIC_IP = "93.184.216.34"
CONNECT_LINE = b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
SNI_BYTES = b"handshake-sni=example.com\x00\x01\x02"
CERT_BYTES = b"origin-cert=example.com\x03\x02\x01"


@asynccontextmanager
async def _origin(payload: bytes, capture: list[bytes]) -> AsyncGenerator[int]:
    """One loopback origin that records the request and answers ``payload``."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        capture.append(await reader.read(65536))
        writer.write(payload)
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _dial_to(port: int, seen: list[tuple[str, int]]) -> Dial:
    """Dial the local origin, recording the target the proxy asked for."""

    async def dial(host: str, target_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        seen.append((host, target_port))
        return await asyncio.open_connection("127.0.0.1", port)

    return dial


@asynccontextmanager
async def _proxy(dial: Dial) -> AsyncGenerator[int]:
    """A running loopback proxy, closed on exit."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=dial)
    try:
        yield server.port
    finally:
        await server.aclose()


async def _connect(port: int, first: bytes, then: bytes | None = None) -> bytes:
    """Open one proxy connection, send ``first``, optionally ``then``, read the answer."""
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(first)
        await writer.drain()
        if then is not None:
            await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)
            writer.write(then)
            await writer.drain()
        return await asyncio.wait_for(reader.read(), 5.0)
    finally:
        writer.close()


async def test_http_absolute_form_forwards_with_logical_host_and_pinned_dial(monkeypatch) -> None:
    """The proxy accepts absolute-form HTTP and dials the pinned IP with Host logical."""
    resolves_in_sequence(monkeypatch, PUBLIC_IP)
    origin_capture: list[bytes] = []
    dial_seen: list[tuple[str, int]] = []
    response = b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n"
    async with _origin(response, origin_capture) as origin_port:
        async with _proxy(_dial_to(origin_port, dial_seen)) as proxy_port:
            answer = await _connect(
                proxy_port,
                b"GET http://example.com/a?b=c HTTP/1.1\r\nHost: attacker.test\r\n\r\n",
            )
    assert answer.startswith(b"HTTP/1.1 204")
    assert origin_capture[0].startswith(b"GET /a?b=c HTTP/1.1\r\n")
    assert b"Host: example.com\r\n" in origin_capture[0]
    assert b"attacker.test" not in origin_capture[0]
    assert origin_capture[0].count(b"GET ") == 1
    assert b"http://example.com" not in origin_capture[0]
    assert dial_seen == [(PUBLIC_IP, 80)]


async def test_connect_tunnel_preserves_client_tls_bytes_and_identity(monkeypatch) -> None:
    """CONNECT tunnels raw bytes verbatim: client SNI and origin cert stay logical."""
    resolves_in_sequence(monkeypatch, PUBLIC_IP)
    origin_capture: list[bytes] = []
    dial_seen: list[tuple[str, int]] = []
    async with _origin(CERT_BYTES, origin_capture) as origin_port:
        async with _proxy(_dial_to(origin_port, dial_seen)) as proxy_port:
            answer = await _connect(proxy_port, CONNECT_LINE, SNI_BYTES)
    assert answer == CERT_BYTES
    assert origin_capture == [SNI_BYTES]
    assert PUBLIC_IP.encode() not in origin_capture[0]
    assert dial_seen == [(PUBLIC_IP, 443)]


def test_door_pins_first_resolution_without_redial(monkeypatch) -> None:
    """A second answer that turns private is never consulted: the pin is the dial."""
    asked = resolves_in_sequence(monkeypatch, PUBLIC_IP, "10.0.0.5")
    target = admit_proxy_target("example.com:443", policy=UNRESTRICTED_HOST_POLICY)
    assert target.pinned_ip == PUBLIC_IP
    assert target.logical_host == "example.com"
    assert asked == ["example.com"]


async def test_connect_refuses_a_private_target_without_dialing(monkeypatch) -> None:
    """The wire refuses a private target before any dial reaches the origin."""
    resolves_in_sequence(monkeypatch, "10.0.0.5")
    dial_seen: list[tuple[str, int]] = []
    async with _proxy(_dial_to(1, dial_seen)) as proxy_port:
        answer = await _connect(proxy_port, CONNECT_LINE)
    assert answer.startswith(b"HTTP/1.1 403")
    assert dial_seen == []


def test_door_refuses_a_private_resolution(monkeypatch) -> None:
    """A host resolving to a private IP is refused before any dial is possible."""
    resolves_in_sequence(monkeypatch, "10.0.0.5")
    with pytest.raises(ValueError, match="SSRF protection"):
        admit_proxy_target("example.com:443", policy=UNRESTRICTED_HOST_POLICY)


def test_door_refuses_a_denied_host() -> None:
    """HostPolicy denial rides the safe_url door, not a proxy-local list."""
    with pytest.raises(ValueError):
        admit_proxy_target("example.com:443", policy=host_policy_for("", "example.com"))


def test_door_refuses_a_resolver_that_answers_nothing(monkeypatch) -> None:
    """An empty resolver answer fails closed instead of dialing the logical host."""
    from tests._assertions import block_private_targets

    block_private_targets(monkeypatch)

    def no_answer(host, *args, **kwargs):
        _ = (host, args, kwargs)
        return []

    monkeypatch.setattr("socket.getaddrinfo", no_answer)
    with pytest.raises(ValueError, match="SSRF protection"):
        admit_proxy_target("example.com:443", policy=UNRESTRICTED_HOST_POLICY)


def test_saturated_resolver_fails_closed(monkeypatch) -> None:
    """A saturated DNS pool refuses fast; the target never reaches a dial."""
    from crawlutron.transport import pinned_dns
    from tests._assertions import block_private_targets

    block_private_targets(monkeypatch)
    parked = ParkedResolver(monkeypatch)
    parked.occupy(pinned_dns, pinned_dns.DNS_WORKERS)
    try:
        with pytest.raises(ValueError, match="SSRF protection: cannot resolve"):
            admit_proxy_target("example.com:443", policy=UNRESTRICTED_HOST_POLICY)
    finally:
        parked.release(pinned_dns)


async def test_non_loopback_bind_is_refused() -> None:
    """The server binds loopback only: a LAN-facing address is refused."""
    from crawlutron.transport.egress_proxy import ProxyBindRefused

    with pytest.raises(ProxyBindRefused):
        await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, host="0.0.0.0", dial=_dial_to(1, []))


async def test_oversized_header_block_is_refused_without_traceback(caplog) -> None:
    """An over-cap header block answers 400; no overrun traceback reaches a log sink."""
    dial_seen: list[tuple[str, int]] = []
    with caplog.at_level(logging.DEBUG):
        async with _proxy(_dial_to(1, dial_seen)) as proxy_port:
            answer = await _connect(proxy_port, b"GET / HTTP/1.1\r\nX-Big: " + b"A" * 70000)
    assert answer.startswith(b"HTTP/1.1 400")
    assert b"Bad-Request" in answer
    assert "LimitOverrunError" not in caplog.text
    assert "Traceback" not in caplog.text
    assert dial_seen == []


async def test_refusal_never_logs_the_target_or_its_secret(caplog) -> None:
    """A wire refusal answers 403 with a fixed reason and logs no target or secret."""
    authority = "example.com:443?token=s3cr3t-value"
    dial_seen: list[tuple[str, int]] = []
    server = await serve_proxy(policy=host_policy_for("", "example.com"), dial=_dial_to(1, dial_seen))
    try:
        with caplog.at_level(logging.DEBUG):
            answer = await _connect(server.port, f"CONNECT {authority} HTTP/1.1\r\n\r\n".encode("ascii"))
    finally:
        await server.aclose()
    assert answer.startswith(b"HTTP/1.1 403")
    assert b"Forbidden" in answer
    assert "s3cr3t-value" not in caplog.text
    assert host_of(authority) not in caplog.text
    assert dial_seen == []


async def test_client_half_close_keeps_the_origin_response_flowing(monkeypatch) -> None:
    """A client write-EOF must not reset the socket still carrying the response."""
    resolves_in_sequence(monkeypatch, PUBLIC_IP)
    head = b"HTTP/1.1 200 OK\r\nContent-Length: 8\r\n\r\nabcd"

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await reader.read(65536)
        writer.write(head)
        await writer.drain()
        try:
            await reader.read()  # EOF when the proxy half-closes; RST on the abort path
        except OSError:
            return
        writer.write(b"efgh")
        await writer.drain()
        writer.close()

    origin = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        async with _proxy(_dial_to(origin.sockets[0].getsockname()[1], [])) as proxy_port:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy_port)
            try:
                writer.write(b"GET http://example.com/x HTTP/1.1\r\nHost: example.com\r\n\r\n")
                await writer.drain()
                first = await asyncio.wait_for(reader.read(1024), 5.0)
                writer.write_eof()
                await writer.drain()
                try:
                    rest = await asyncio.wait_for(reader.read(), 5.0)
                except OSError:
                    rest = b""
            finally:
                writer.close()
    finally:
        origin.close()
        await origin.wait_closed()
    assert first + rest == head + b"efgh"
