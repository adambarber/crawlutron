"""Egress proxy lifecycle: the registry holds exactly the servers still listening."""

from __future__ import annotations

import asyncio

import pytest

from crawlutron.runtime import process_state
from crawlutron.transport.egress_proxy import serve_proxy
from tests._policy_factories import UNRESTRICTED_HOST_POLICY

_OWNER = "crawlutron.transport.egress_proxy"


async def test_serve_forever_shutdown_drops_the_registration() -> None:
    """A proxy closed through serve_forever leaves no stale owner: reset cannot double-close."""
    assert not process_state.owner_has_state(_OWNER)
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY)
    assert process_state.owner_has_state(_OWNER)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.serve_forever(), 0.05)
    assert not server.is_live
    assert not process_state.owner_has_state(_OWNER)
    process_state.reset_all_process_state()


async def test_aclose_drops_the_registration() -> None:
    """The async close door deregisters too: no stale owner survives a clean close."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY)
    await server.aclose()
    assert not server.is_live
    assert not process_state.owner_has_state(_OWNER)
    process_state.reset_all_process_state()


async def test_process_reset_closes_a_live_listener() -> None:
    """A registered live listener is still closed by the process reset door."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY)
    assert server.is_live
    process_state.reset_all_process_state()
    assert not server.is_live
    assert not process_state.owner_has_state(_OWNER)
