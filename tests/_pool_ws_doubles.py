"""Pool and websocket doubles shared across the transport/ test modules.

Declared support surface (#476): moved verbatim from the sibling test modules
so a test imports these from here, not from another test's internals.
"""

from __future__ import annotations

import asyncio

__all__ = ["FakeClient", "FakeWS", "bind_to_running_loop"]


class FakeClient:
    def __init__(self):
        self.is_closed = False
        self._bound_loop = None

    async def aclose(self):
        self.is_closed = True


def bind_to_running_loop(client) -> None:
    """Bind a fake client to the running loop, or leave it unbound if there is none.

    An unbound fake is how the pool's "closed from the loop it was made on"
    question gets answered honestly."""
    try:
        client._bound_loop = asyncio.get_running_loop()
    except RuntimeError:
        client._bound_loop = None


class FakeWS:
    """A stand-in for Playwright's ``WebSocketRoute``: ``url``/``close()``/``connect_to_server()``."""

    def __init__(self, url):
        self.url = url
        self.closed_count = 0
        self.connect_calls = 0

    async def close(self, **kwargs):
        self.closed_count += 1

    def connect_to_server(self):
        self.connect_calls += 1
        return self
