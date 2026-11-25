"""Egress proxy sidecar for dual-homed container deployment (internal+egress networks)."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import socket
import sys

from crawlutron.config.settings_cache import get_cached_settings
from crawlutron.transport.egress_proxy import serve_proxy
from safe_url import HostPolicy

_LOGGER = logging.getLogger(__name__)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Sidecar binds all interfaces (internal network); intentional for dual-homed deployment.
    # Parse optional environment string into IPv4Address; default to INADDR_ANY when absent.
    bind_host_env = os.environ.get("EGRESS_PROXY_BIND_HOST")
    if bind_host_env is not None:
        try:
            sidecar_bind = ipaddress.IPv4Address(bind_host_env)
        except ValueError as e:
            _LOGGER.error("Invalid EGRESS_PROXY_BIND_HOST: %s", e)
            return 1
    else:
        sidecar_bind = ipaddress.IPv4Address(socket.INADDR_ANY)

    bind_port = int(os.environ.get("EGRESS_PROXY_BIND_PORT", "8888"))

    # Policy for outbound connections (from egress network), built from cached
    # settings just like the in-process proxy.
    settings = get_cached_settings()
    policy = HostPolicy.from_settings(settings)

    _LOGGER.info("Starting egress proxy sidecar on %s:%s", sidecar_bind, bind_port)

    try:
        server = await serve_proxy(policy=policy, port=bind_port, sidecar_bind=sidecar_bind)

        _LOGGER.info("Egress proxy sidecar listening on %s:%s", sidecar_bind, server.port)

        # Run forever
        async with server._server:
            await server._server.serve_forever()
    except OSError as e:
        _LOGGER.error("Failed to start proxy: %s", e)
        return 1
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
