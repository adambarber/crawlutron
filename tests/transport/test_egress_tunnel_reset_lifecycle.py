"""A reset must own every CONNECT tunnel it created (#1485)."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

import pytest

from crawlutron.transport import egress_proxy_runtime
from crawlutron.transport.egress_proxy import ProxyServer, serve_proxy
from tests._assertions import resolves_in_sequence
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

Dial = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]
CONNECT = b"CONNECT example.com:443 HTTP/1.1\r\n\r\n"
PUBLIC_IP = "93.184.216.34"
_PHASES = ("connect", "mid_body", "half_close")


@asynccontextmanager
async def _stalled_origin(seen: list[asyncio.StreamWriter]) -> AsyncGenerator[int]:
    """An origin whose upstream socket stays open until the proxy closes it."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        seen.append(writer)
        await reader.read()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


def _dial_to(port: int) -> Dial:
    """Dial the stalled loopback origin."""

    async def dial(_host: str, _target_port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        return await asyncio.open_connection("127.0.0.1", port)

    return dial


@asynccontextmanager
async def _proxy(dial: Dial) -> AsyncGenerator[ProxyServer]:
    """A loopback proxy whose close is the test's sole ownership door."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY, dial=dial, register=False)
    try:
        yield server
    finally:
        await server.aclose()


@pytest.mark.parametrize("phase", _PHASES)
async def test_aclose_reaps_a_live_connect_tunnel(monkeypatch: pytest.MonkeyPatch, phase: str) -> None:
    """Kills mutant: no handler tracking (aclose waits forever on the relay)."""
    resolves_in_sequence(monkeypatch, PUBLIC_IP)
    upstream: list[asyncio.StreamWriter] = []
    async with _stalled_origin(upstream) as origin_port:
        async with _proxy(_dial_to(origin_port)) as server:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            try:
                writer.write(CONNECT)
                await writer.drain()
                assert (await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5.0)).startswith(
                    b"HTTP/1.1 200"
                )
                assert upstream, "no upstream socket was dialed"
                if phase == "mid_body":
                    writer.write(b"tls-client-bytes")
                    await writer.drain()
                elif phase == "half_close":
                    writer.write_eof()
                await asyncio.wait_for(server.aclose(), 3.0)
                assert server.active_handlers == 0, "a relay task survived the close"
                assert 0 < server.tunnel_lifetime_seconds < 60 * 60
                assert upstream[0].is_closing(), "the upstream socket survived the close"
                await asyncio.wait_for(server.aclose(), 3.0)
            finally:
                writer.close()


def _holding_origin() -> tuple[socket.socket, list[socket.socket]]:
    """A threaded origin whose accepted socket stays open until the proxy closes it."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    accepted: list[socket.socket] = []

    def hold() -> None:
        connection, _ = listener.accept()
        accepted.append(connection)
        try:
            while connection.recv(4096):
                pass
        except OSError:
            pass
        finally:
            connection.close()

    threading.Thread(target=hold, daemon=True).start()
    return listener, accepted


def test_runtime_reset_reaps_a_live_tunnel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Kills mutant: reset suppresses the close timeout and drops a live tunnel."""
    monkeypatch.setenv("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "0")
    real_getaddrinfo = socket.getaddrinfo

    def _loopback_aware_resolve(host, port, *args, **kwargs):
        if host == "127.0.0.1":
            return real_getaddrinfo(host, port, *args, **kwargs)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_IP, port))]

    monkeypatch.setattr(socket, "getaddrinfo", _loopback_aware_resolve)
    listener, accepted = _holding_origin()
    origin_port = listener.getsockname()[1]
    captured: list[ProxyServer] = []
    real_serve = egress_proxy_runtime.serve_proxy

    async def capture(**kwargs) -> ProxyServer:
        server = await real_serve(dial=_dial_to(origin_port), **kwargs)
        captured.append(server)
        return server

    monkeypatch.setattr(egress_proxy_runtime, "serve_proxy", capture)
    url = egress_proxy_runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY)
    client = socket.create_connection(("127.0.0.1", int(url.rsplit(":", 1)[1])), timeout=5)
    try:
        client.sendall(CONNECT)
        response = bytearray()
        while b"\r\n\r\n" not in response:
            chunk = client.recv(4096)
            if not chunk:
                break
            response.extend(chunk)
        assert bytes(response).startswith(b"HTTP/1.1 200")
        deadline = 2.0
        while not accepted and deadline > 0:
            time.sleep(0.01)
            deadline -= 0.01
        assert accepted, "the tunnel never reached the origin"
        assert captured[0].active_handlers == 1
        egress_proxy_runtime.reset_egress_proxy()
        assert captured[0].active_handlers == 0, "a relay task survived the runtime reset"
        assert accepted[0].fileno() == -1, "the upstream socket survived the runtime reset"
        egress_proxy_runtime.reset_egress_proxy()
    finally:
        client.close()
        listener.close()
        egress_proxy_runtime.reset_egress_proxy()
