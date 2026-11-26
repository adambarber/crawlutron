"""Dedicated-loop egress proxy: its creator owns the close on its own loop."""

from __future__ import annotations

import asyncio
import threading

from crawlutron.runtime import process_state
from crawlutron.transport import egress_proxy_runtime
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

_EGRESS_OWNER = "crawlutron.transport.egress_proxy"
_RUNTIME_OWNER = "crawlutron.transport.egress_proxy_runtime"


def _capture_started(monkeypatch) -> list[asyncio.AbstractServer]:
    """Collect the listeners ``asyncio.start_server`` creates in this test."""
    created: list[asyncio.AbstractServer] = []
    real_start = asyncio.start_server

    async def capture(client_connected_cb, host=None, port=None, **kwds):
        server = await real_start(client_connected_cb, host, port, **kwds)
        created.append(server)
        return server

    monkeypatch.setattr(asyncio, "start_server", capture)
    return created


def _sweep_and_snapshot(monkeypatch, target: asyncio.AbstractServer, owner: str) -> tuple[list[str], bool]:
    """Reset the registry; capture close threads and owner state before cleanup."""
    closed_on: list[str] = []
    real_close = asyncio.Server.close

    def spy(self: asyncio.Server) -> None:
        if self is target:
            closed_on.append(threading.current_thread().name)
        real_close(self)

    monkeypatch.setattr(asyncio.Server, "close", spy)
    observed: list[str] = []
    held = True
    try:
        process_state.reset_all_process_state()
        observed = list(closed_on)
        held = process_state.owner_has_state(owner)
    finally:
        egress_proxy_runtime.reset_egress_proxy()
    return observed, held


def _assert_closed_off_main(observed: list[str]) -> None:
    assert len(observed) == 1, observed
    assert observed[0] != threading.main_thread().name, observed


def test_runtime_proxy_closed_only_on_own_loop(monkeypatch) -> None:
    """The global egress owner must not close a dedicated-loop listener."""
    assert not process_state.owner_has_state(_EGRESS_OWNER)
    created = _capture_started(monkeypatch)
    egress_proxy_runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY)
    assert len(created) == 1
    assert not process_state.owner_has_state(_EGRESS_OWNER)
    observed, held = _sweep_and_snapshot(monkeypatch, created[0], _EGRESS_OWNER)
    assert not held
    assert not created[0].is_serving()
    _assert_closed_off_main(observed)


def test_registry_sweep_closes_its_own_runtime_proxy(monkeypatch) -> None:
    """The runtime owns its registration: the sweep closes its listener on the
    creating loop without any pooled_clients import."""
    egress_proxy_runtime.reset_egress_proxy()
    assert not process_state.owner_has_state(_RUNTIME_OWNER)
    created = _capture_started(monkeypatch)
    egress_proxy_runtime.egress_proxy_url(UNRESTRICTED_HOST_POLICY)
    assert process_state.owner_has_state(_RUNTIME_OWNER)
    observed, held = _sweep_and_snapshot(monkeypatch, created[0], _RUNTIME_OWNER)
    assert not held
    assert not created[0].is_serving()
    _assert_closed_off_main(observed)
