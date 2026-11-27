"""A failed dial through the pooled client must leave no live socket behind: the unraisable-teardown flake
family.
"""

import asyncio
import gc
import socket

import httpx
import pytest

from tests._fetcher_factories import reserved_refusal_socket
from tests._policy_factories import canonical


def _connected_dial_sockets() -> list[tuple[tuple[object, ...], tuple[object, ...]]]:
    """Live sockets with a peer address -- what __del__ warns about at teardown."""
    live: list[tuple[tuple[object, ...], tuple[object, ...]]] = []
    for obj in gc.get_objects():
        if not isinstance(obj, socket.socket) or obj.fileno() == -1:
            continue
        try:
            peer = obj.getpeername()
        except OSError:
            continue
        if peer:
            live.append((obj.getsockname(), peer))
    return live


def _assert_pool_released() -> None:
    """The seam's contract, asserted rather than inferred from a GC sweep.

    A socket sweep alone is trivially satisfied where a refused dial leaves no
    socket to find, so the release itself is pinned here too."""
    from crawlutron.transport import pooled_clients

    released = pooled_clients.http_singleton().current
    assert released is None, f"reset seam left a pooled client behind: {released!r}"


async def _reclaim_via_reset_seam() -> None:
    """The suite's teardown seam, end to end: reset, then drain the close the
    reset scheduled on this loop (a sync-only reset leaves the close pending)."""
    from crawlutron.runtime import process_state
    from crawlutron.transport import pooled_clients

    process_state.reset_all_process_state()
    await pooled_clients.drain_pending_client_closes()


async def _refused_dial_fetch(port: int) -> None:
    """The real pooled-client dial to an owned port that never accepts."""
    from crawlutron.transport.fetchers import default_http_fetcher

    with pytest.raises(httpx.ConnectError):
        await default_http_fetcher(canonical(f"http://127.0.0.1:{port}/"), {}, 5)


async def _refused_dial_fetch_then_reset() -> None:
    """Fetch, then reset inside the fetch's own live loop."""
    with reserved_refusal_socket() as sock:
        await _refused_dial_fetch(int(sock.getsockname()[1]))
    await _reclaim_via_reset_seam()


async def test_a_failed_dial_leaves_no_connected_socket_after_the_reset_seam():
    """After a refused dial, no socket survives the reset seam."""
    with reserved_refusal_socket() as sock:
        await _refused_dial_fetch(int(sock.getsockname()[1]))
    await _reclaim_via_reset_seam()
    _assert_pool_released()
    gc.collect()
    leaked = _connected_dial_sockets()
    assert not leaked, f"failed dial leaked connected sockets: {leaked}"


async def test_reset_on_the_fetchs_own_live_loop_closes_the_pool():
    """Reset running inside the owning loop must not deadlock the close (the
    unraisable __del__ flake family): the socket is gone when the loop exits."""
    await _refused_dial_fetch_then_reset()
    _assert_pool_released()
    gc.collect()
    leaked = _connected_dial_sockets()
    assert not leaked, f"live-loop reset leaked sockets: {leaked}"


def _dial_from_a_dead_loop() -> None:
    """Run one refused dial in a sibling loop that exits before the reset."""
    with reserved_refusal_socket() as sock:
        asyncio.run(_refused_dial_fetch(int(sock.getsockname()[1])))


async def test_a_socket_from_a_dead_loop_is_reclaimed_by_the_reset_seam():
    """The battery shape: the fetch's loop is gone when the close runs."""
    await asyncio.to_thread(_dial_from_a_dead_loop)
    await _reclaim_via_reset_seam()
    _assert_pool_released()
    gc.collect()
    leaked = _connected_dial_sockets()
    assert not leaked, f"dead-loop pooled client leaked sockets: {leaked}"


async def test_refusal_port_guard_lives_through_the_dial() -> None:
    """The owning guard is live before and after the refused dial."""
    with reserved_refusal_socket() as sock:
        await _refused_dial_fetch(int(sock.getsockname()[1]))
        assert sock.fileno() != -1, "port guard released before the dial completed"
        await _reclaim_via_reset_seam()
        _assert_pool_released()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-n0"]))
