"""Loopback proxy lifecycle for the synchronous pooled-client builder.

The builder cannot await ``serve_proxy``, so one listener runs on a dedicated
loop; httpx connects to it over TCP from whichever loop owns the client.
"""

from __future__ import annotations

import asyncio
import threading
from contextlib import suppress

from crawlutron.transport.egress_proxy import ProxyServer, serve_proxy
from safe_url.host_policy import HostPolicy

__all__ = ["egress_proxy_url", "has_egress_proxy", "reset_egress_proxy"]

_loop: asyncio.AbstractEventLoop | None = None
_server: ProxyServer | None = None
_url: str | None = None
_lock = threading.Lock()


def reset_egress_proxy() -> None:
    """Drop the listener; the dedicated loop stays reusable for the next build."""
    global _server, _url
    with _lock:
        loop = _loop
        if _server is not None and _server.is_live and loop is not None:
            with suppress(Exception):
                asyncio.run_coroutine_threadsafe(_server.aclose(), loop).result(timeout=5.0)
        _server, _url = None, None


def egress_proxy_url(policy: HostPolicy) -> str:
    """Return the loopback proxy URL, starting one listener on its own loop."""
    global _loop, _server, _url
    with _lock:
        if _url is not None and _server is not None and _server.is_live:
            return _url
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
        server = asyncio.run_coroutine_threadsafe(serve_proxy(policy=policy), _loop).result(timeout=10.0)
        _server, _url = server, f"http://127.0.0.1:{server.port}"
        return _url


def has_egress_proxy() -> bool:
    """Whether a proxy listener or its cached URL is still held."""
    return _url is not None
