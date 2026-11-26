"""The loopback API ingress relay has a fixed upstream and a clean lifecycle."""

from __future__ import annotations

import asyncio
import ipaddress
import socket

import pytest

from crawlutron_admit.task_scope import Grace


def _free_port() -> int:
    """Reserve an ephemeral loopback port for an isolated relay listener."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


async def _echo_server(sink: list[bytes] | None = None) -> tuple[asyncio.Server, int]:
    """Start a loopback echo server, recording what it receives when given a sink."""

    async def _echo(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        payload = await reader.read(4096)
        if sink is not None:
            sink.append(payload)
        writer.write(b"echo:" + payload)
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(_echo, "127.0.0.1", 0)
    return server, int(server.sockets[0].getsockname()[1])


def test_ingress_config_defaults_and_rejects_bad_values() -> None:
    """Only the fixed upstream and a valid bind are admitted."""
    from scripts.api_ingress_sidecar import parse_ingress_config

    config = parse_ingress_config({})
    assert config[0] == ipaddress.IPv4Address(socket.INADDR_ANY)
    assert (config[2], config[3]) == ("crawlutron", 8765)

    bad = (
        {"API_INGRESS_BIND_HOST": "127.0.0.1"},
        {"API_INGRESS_BIND_HOST": "8.8.8.8"},
        {"API_INGRESS_BIND_HOST": "not-an-ip"},
        {"API_INGRESS_BIND_HOST": ""},
        {"API_INGRESS_BIND_PORT": ""},
        {"API_INGRESS_BIND_PORT": "0"},
        {"API_INGRESS_BIND_PORT": "70000"},
        {"API_INGRESS_BIND_PORT": "1 2"},
        {"API_INGRESS_BIND_PORT": "-1"},
        {"API_INGRESS_UPSTREAM_HOST": ""},
        {"API_INGRESS_UPSTREAM_HOST": "user@evil/"},
        {"API_INGRESS_UPSTREAM_HOST": "evil:8765"},
        {"API_INGRESS_UPSTREAM_PORT": ""},
        {"API_INGRESS_UPSTREAM_PORT": "nope"},
        {"API_INGRESS_UPSTREAM_PORT": "0"},
        {"API_INGRESS_UPSTREAM_PORT": "70000"},
    )
    for env in bad:
        with pytest.raises(ValueError):
            parse_ingress_config(env)


async def test_relay_pipes_bytes_and_half_close() -> None:
    """The relay carries bytes both ways and ends cleanly on client half-close."""
    from scripts.api_ingress_sidecar import parse_ingress_config, serve_forever, start

    sink: list[bytes] = []
    upstream, upstream_port = await _echo_server(sink)
    async with upstream:
        config = parse_ingress_config(
            {
                "API_INGRESS_BIND_HOST": "0.0.0.0",
                "API_INGRESS_BIND_PORT": str(_free_port()),
                "API_INGRESS_UPSTREAM_HOST": "127.0.0.1",
                "API_INGRESS_UPSTREAM_PORT": str(upstream_port),
            }
        )
        server, port, scope = await start(config)
        serving = scope.spawn_now(lambda: serve_forever(server, scope))
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
            try:
                writer.write(b"ping")
                await writer.drain()
                writer.write_eof()
                assert await asyncio.wait_for(reader.read(4096), 5) == b"echo:ping"
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await scope.stop(serving, Grace(2))
        assert not server.is_serving()
        assert sink == [b"ping"]
        assert scope.registry.active.value == 0


async def test_relay_ignores_client_selected_upstream() -> None:
    """Client-supplied URL, Host and payload never change the fixed dial target."""
    from scripts.api_ingress_sidecar import parse_ingress_config, serve_forever, start

    sink: list[bytes] = []
    upstream, upstream_port = await _echo_server(sink)
    attacker_sink: list[bytes] = []
    attacker, attacker_port = await _echo_server(attacker_sink)
    async with upstream, attacker:
        config = parse_ingress_config(
            {
                "API_INGRESS_BIND_HOST": "0.0.0.0",
                "API_INGRESS_BIND_PORT": str(_free_port()),
                "API_INGRESS_UPSTREAM_HOST": "127.0.0.1",
                "API_INGRESS_UPSTREAM_PORT": str(upstream_port),
            }
        )
        server, port, scope = await start(config)
        serving = scope.spawn_now(lambda: serve_forever(server, scope))
        hostile = (
            f"GET http://attacker.invalid:{attacker_port}/ HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{attacker_port}\r\n"
            "X-Forward-To: 127.0.0.1\r\n\r\n"
        ).encode()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 5)
            try:
                writer.write(hostile)
                await writer.drain()
                assert await asyncio.wait_for(reader.read(4096), 5) == b"echo:" + hostile
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await scope.stop(serving, Grace(2))
        assert not server.is_serving()
        assert sink == [hostile]
        assert attacker_sink == []
        assert scope.registry.active.value == 0
