"""Loopback proxy lifecycle for the synchronous pooled-client builder.

The builder cannot await ``serve_proxy``, so one listener runs on a dedicated
loop; httpx connects to it over TCP from whichever loop owns the client.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import TYPE_CHECKING

from crawlutron.runtime import process_state
from crawlutron.transport.egress_proxy import ProxyServer, serve_proxy
from safe_url.host_policy import HostPolicy

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings

__all__ = ["egress_proxy_url", "has_egress_proxy", "reset_egress_proxy"]

_LOGGER = logging.getLogger(__name__)
_RESET_CLOSE_TIMEOUT = 5.0

_loop: asyncio.AbstractEventLoop | None = None
_server: ProxyServer | None = None
_url: str | None = None
_lock = threading.Lock()


def _default_settings() -> CrawlerSettings:
    """Get default crawler settings for the egress door."""
    from crawlutron.config.settings_cache import get_cached_settings

    return get_cached_settings()


async def _abort_handlers(server: ProxyServer) -> None:
    """Last-resort socket abort, scheduled on the loop that created them."""
    server.abort_handlers()


def reset_egress_proxy() -> None:
    """Drop the listener; the dedicated loop stays reusable for the next build."""
    global _loop, _server, _url
    with _lock:
        loop, server = _loop, _server
        _server, _url = None, None
    if server is None or loop is None or not server.is_live:
        return
    try:
        asyncio.run_coroutine_threadsafe(server.aclose(), loop).result(timeout=_RESET_CLOSE_TIMEOUT)
    except TimeoutError:
        # The creating loop owns every socket; force the abort there, never
        # from this thread, and never by swallowing the timeout.
        _LOGGER.warning("egress proxy reset aborted its handlers on the creating loop")
        asyncio.run_coroutine_threadsafe(_abort_handlers(server), loop)


def egress_proxy_url(policy: HostPolicy, settings: CrawlerSettings | None = None) -> str:
    """Return the egress proxy URL: empty setting → in-process loopback listener;
    configured → sidecar endpoint, returned as-is without starting a listener."""
    global _loop, _server, _url
    if settings is None:
        settings = _default_settings()
    configured = settings.egress_proxy_url
    if configured:
        return configured
    with _lock:
        if _url is not None and _server is not None and _server.is_live:
            return _url
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
        server = asyncio.run_coroutine_threadsafe(serve_proxy(policy=policy, register=False), _loop).result(
            timeout=10.0
        )
        _server, _url = server, f"http://127.0.0.1:{server.port}"
        return _url


def has_egress_proxy() -> bool:
    """Whether a proxy listener or its cached URL is still held."""
    global _url
    return _url is not None


def _has() -> bool:
    """Whether the dedicated-loop listener is still held."""
    return _server is not None


process_state.register(__name__, reset_egress_proxy, _has)
