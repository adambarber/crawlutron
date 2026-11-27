"""Loopback API ingress relay: a fixed upstream, never a caller-selected dial."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import sys
from collections.abc import Mapping

from crawlutron.config.sidecar import SidecarConfig
from crawlutron_admit.task_scope import Grace, Handle, NamedRegistry, TaskScope
from crawlutron_admit.values import Count, Duration

_LOGGER = logging.getLogger(__name__)

# bind address, bind port, fixed upstream host, fixed upstream port.
IngressConfig = tuple[ipaddress.IPv4Address, int, str, int]


def parse_ingress_config(env: Mapping[str, str]) -> IngressConfig:
    """Project the shared typed door onto the relay's existing tuple contract."""
    config = SidecarConfig.for_ingress(env)
    return (
        config.bind_host,
        config.bind_port,
        config.upstream_host,
        config.upstream_port,
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
    """Dial, pump and close as one governed operation; a dial fault still closes the client."""
    _, _, upstream_host, upstream_port = config
    upstream_writer: asyncio.StreamWriter | None = None
    pumps: tuple[Handle, ...] = ()
    try:
        try:
            upstream_reader, upstream = await asyncio.open_connection(upstream_host, upstream_port)
        except OSError:
            _LOGGER.warning("api ingress could not reach the fixed upstream")
            return
        upstream_writer = upstream
        pumps = (
            scope.spawn_now(lambda: _pump(reader, upstream)),
            scope.spawn_now(lambda: _pump(upstream_reader, writer)),
        )
        await asyncio.gather(*(handle.task for handle in pumps), return_exceptions=True)
    finally:
        streams = tuple(stream for stream in (writer, upstream_writer) if stream is not None)
        for stream in streams:
            stream.close()
        await asyncio.gather(*(stream.wait_closed() for stream in streams), return_exceptions=True)
        for handle in pumps:
            scope.stop_now(handle)


async def start(
    config: IngressConfig, *, sock: socket.socket | None = None
) -> tuple[asyncio.Server, int, TaskScope]:
    """Bind the listener; return it, its actual port, and its lifecycle scope."""
    bind_host, bind_port, _, _ = config
    scope = TaskScope.process(
        registry=NamedRegistry("api-ingress", Count(256), Duration(5)),
        grace=Grace(5),
    )

    def _accept(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        handle = scope.spawn_now(lambda: relay_connection(reader, writer, config, scope))

        def _settled(task: asyncio.Task[object]) -> None:
            if not task.cancelled():
                task.exception()
            scope.stop_now(handle)

        handle.task.add_done_callback(_settled)

    if sock is None:
        server = await asyncio.start_server(_accept, str(bind_host), bind_port)
    else:
        server = await asyncio.start_server(_accept, sock=sock)
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
