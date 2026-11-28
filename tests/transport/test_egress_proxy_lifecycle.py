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
    now_live = bool(server.is_live)
    assert not now_live
    assert not process_state.owner_has_state(_OWNER)


async def test_two_close_paths_close_once_with_spent_waiters(monkeypatch: pytest.MonkeyPatch) -> None:
    """A second lifecycle path must never reach Server.close once waiters are spent."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY)
    target = server._server
    calls: list[bool] = []
    real_close = asyncio.Server.close

    def spy_close(self: asyncio.Server) -> None:
        if self is target:
            calls.append(vars(self)["_waiters"] is None)
        real_close(self)

    monkeypatch.setattr(asyncio.Server, "close", spy_close)

    await server.aclose()
    assert calls == [False], calls
    assert vars(target)["_waiters"] is None

    # Reproduce the reset interleaving: is_serving() True while the wakeup
    # waiters are spent, so a liveness check cannot guard the second close.
    vars(target)["_serving"] = True
    await server.serve_forever()

    assert len(calls) == 1, calls
    assert not process_state.owner_has_state(_OWNER)


async def test_serve_forever_owns_the_single_close(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the lifecycle door closes; no second call reaches _wakeup's None state."""
    server = await serve_proxy(policy=UNRESTRICTED_HOST_POLICY)
    target = server._server
    calls: list[tuple[str, bool]] = []
    real_close = asyncio.Server.close
    real_wait = asyncio.Server.wait_closed

    def waiters_spent(server: asyncio.Server) -> bool:
        """Read the stdlib wakeup marker through the instance dict (untyped attr)."""
        return vars(server)["_waiters"] is None

    def spy_close(self: asyncio.Server) -> None:
        if self is target:
            calls.append(("close", waiters_spent(self)))
        real_close(self)

    async def spy_wait(self: asyncio.Server) -> None:
        if self is target:
            calls.append(("wait", waiters_spent(self)))
        await real_wait(self)

    monkeypatch.setattr(asyncio.Server, "close", spy_close)
    monkeypatch.setattr(asyncio.Server, "wait_closed", spy_wait)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(server.serve_forever(), 0.05)
    process_state.reset_all_process_state()

    closes = [entry for entry in calls if entry[0] == "close"]
    assert len(closes) == 1, calls
    assert closes[0][1] is False, calls
