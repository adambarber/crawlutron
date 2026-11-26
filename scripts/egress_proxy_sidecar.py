"""Egress proxy sidecar for dual-homed container deployment (internal+egress networks)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from crawlutron.config.settings_cache import get_cached_settings
from crawlutron.config.sidecar import SidecarConfig
from crawlutron.transport.egress_proxy import serve_proxy

_LOGGER = logging.getLogger(__name__)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        config = SidecarConfig.for_egress(os.environ, get_cached_settings())
    except ValueError as exc:
        _LOGGER.error("Refusing to start egress proxy sidecar: %s", exc)
        return 1

    _LOGGER.info("Starting egress proxy sidecar on %s:%s", config.bind_host, config.bind_port)
    try:
        server = await serve_proxy(
            policy=config.egress_policy(), port=config.bind_port, sidecar_bind=config.bind_host
        )
        _LOGGER.info("Egress proxy sidecar listening on %s:%s", config.bind_host, server.port)
        await server.serve_forever()
    except OSError as exc:
        _LOGGER.error("Failed to start proxy: %s", exc)
        return 1
    except KeyboardInterrupt:
        _LOGGER.info("Shutting down")
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
