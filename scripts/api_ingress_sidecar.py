"""Loopback API ingress relay: a fixed upstream, never a caller-selected dial."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import re
import socket
import sys
from collections.abc import Mapping

from crawlutron_admit.task_scope import Grace, NamedRegistry, TaskScope
from crawlutron_admit.values import Count, Duration

_LOGGER = logging.getLogger(__name__)
_HOST_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,251}[A-Za-z0-9])?\Z")
_ANY = ipaddress.IPv4Address(socket.INADDR_ANY)

# bind address, bind port, fixed upstream host, fixed upstream port.
IngressConfig = tuple[ipaddress.IPv4Address, int, str, int]


def _port(raw: str | None, name: str, default: int) -> int:
    """Parse a fail-closed TCP port in 1..65535."""
    text = raw if raw is not None else str(default)
    try:
        value = int(text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a decimal port, got {text!r}") from exc
    if not 1 <= value <= 65535:
        raise ValueError(f"{name} must be in 1..65535, got {value}")
    return value


def parse_ingress_config(env: Mapping[str, str]) -> IngressConfig:
    """Admit only the unspecified bind and a well-formed fixed upstream."""
    bind_raw = env.get("API_INGRESS_BIND_HOST")
    if bind_raw is None:
        bind_host = _ANY
    else:
        try:
            bind_host = ipaddress.IPv4Address(bind_raw)
        except ValueError as exc:
            raise ValueError(f"bad API_INGRESS_BIND_HOST {bind_raw!r}") from exc
        if bind_host != _ANY:
            raise ValueError("API_INGRESS_BIND_HOST must be 0.0.0.0")

    host = env.get("API_INGRESS_UPSTREAM_HOST", "crawlutron")
    if not (len(host) <= 253 and _HOST_RE.fullmatch(host)):
        raise ValueError(f"bad API_INGRESS_UPSTREAM_HOST {host!r}")

    return (
        bind_host,
        _port(env.get("API_INGRESS_BIND_PORT"), "API_INGRESS_BIND_PORT", 8765),
        host,
        _port(env.get("API_INGRESS_UPSTREAM_PORT"), "API_INGRESS_UPSTREAM_PORT", 8765),
    )


async def _pump(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
    """Forward one direction and half-close it on EOF."""
    try:
        while data := await src.read(65536):
            dst.write(data)
            await dst.drain()
    except (OSError, ConnectionError):
        _LOGGER.debug("ingress stream ended")
    finally:
        if not dst.is_closing():
            dst.write_eof()


async def relay_connection(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, config: IngressConfig, scope: TaskScope
) -> None:
    """Pipe one accepted client to the fixed upstream until both halves close."""
    _, _, upstream_host, upstream_port = config
    upstream_reader, upstream_writer = await asyncio.open_connection(upstream_host, upstream_port)
    pumps = (
        scope.spawn_now(lambda: _pump(reader, upstream_writer)),
        scope.spawn_now(lambda: _pump(upstream_reader, writer)),
    )
    try:
        await asyncio.gather(*(handle.task for handle in pumps), return_exceptions=True)
    finally:
        for stream in (writer, upstream_writer):
            stream.close()
        await asyncio.gather(writer.wait_closed(), upstream_writer.wait_closed(), return_exceptions=True)
        for handle in pumps:
            scope.stop_now(handle)


async def start(config: IngressConfig) -> tuple[asyncio.Server, int, TaskScope]:
    """Bind the listener; return it, its actual port, and its lifecycle scope."""
    bind_host, bind_port, _, _ = config
    scope = TaskScope.process(
        registry=NamedRegistry("api-ingress", Count(256), Duration(5)),
        grace=Grace(5),
    )

    def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        scope.spawn_now(lambda: relay_connection(reader, writer, config, scope))

    server = await asyncio.start_server(_accept, str(bind_host), bind_port)
    return server, int(server.sockets[0].getsockname()[1]), scope


async def serve_forever(server: asyncio.Server, scope: TaskScope) -> None:
    """Serve until cancelled, then close every admitted relay through the door."""
    async with scope:
        try:
            await server.serve_forever()
        finally:
            server.close()
            await server.wait_closed()


async def main() -> int:
    """Run the relay from fail-closed environment configuration."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = parse_ingress_config(os.environ)
    except ValueError as exc:
        _LOGGER.error("Refusing to start API ingress: %s", exc)
        return 1

    server, port, scope = await start(config)
    _LOGGER.info(
        "API ingress listening on %s:%s -> %s:%s",
        config[0],
        port,
        config[2],
        config[3],
    )
    await serve_forever(server, scope)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
