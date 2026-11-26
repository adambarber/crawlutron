"""Dedicated-loop egress proxy: its creator owns the close on its own loop."""

from __future__ import annotations

import asyncio
import threading

from crawlutron.runtime import process_state
from crawlutron.transport import egress_proxy_runtime
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

_EGRESS_OWNER = "crawlutron.transport.egress_proxy"


def test_runtime_proxy_closed_only_on_own_loop(monkeypatch) -> None:
    """The global egress owner must not close a dedicated-loop listener."""
    assert not process_state.owner_has_state(_EGRESS_OWNER)
    created: list[asyncio.AbstractServer] = []
    real_start = asyncio.start_server

    async def capture(client_connected_cb, host=None, port=None, **kwds):
        server = await real_start(client_connected_cb, host, port, **kwds)
        created.append(server)
        return server

    monkeypatch.setattr(asyncio, "start_server", capture)
    egress_proxy_runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY)
    assert len(created) == 1
    assert not process_state.owner_has_state(_EGRESS_OWNER)

    target = created[0]
    closed_on: list[str] = []
    real_close = asyncio.Server.close

    def spy(self: asyncio.Server) -> None:
        if self is target:
            closed_on.append(threading.current_thread().name)
        real_close(self)

    monkeypatch.setattr(asyncio.Server, "close", spy)
    try:
        process_state.reset_all_process_state()
    finally:
        egress_proxy_runtime.reset_egress_proxy()

    assert len(closed_on) == 1, closed_on
    assert closed_on[0] != threading.main_thread().name, closed_on
