"""Egress proxy sidecar for dual-homed container deployment (internal+egress networks)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from crawlutron.transport.egress_proxy import CONTAINER_INTERNAL_BIND_ALL, serve_proxy_sidecar
from safe_url import HostPolicy
from tests.chokepoint_paths import REPO_ROOT

_LOGGER = logging.getLogger(__name__)


async def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # Ensure repo root is on sys.path for imports
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # Sidecar binds on all interfaces in the container (internal network);
    # binding all interfaces is intentional for dual-homed deployment.
    bind_host = os.environ.get("EGRESS_PROXY_BIND_HOST", CONTAINER_INTERNAL_BIND_ALL)
    bind_port = int(os.environ.get("EGRESS_PROXY_BIND_PORT", "8888"))

    # Policy for outbound connections (from egress network)
    policy = HostPolicy.unrestricted()

    _LOGGER.info("Starting egress proxy sidecar on %s:%s", bind_host, bind_port)

    try:
        server = await serve_proxy_sidecar(policy=policy, host=bind_host, port=bind_port)

        _LOGGER.info("Egress proxy sidecar listening on %s:%s", bind_host, server.port)

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
