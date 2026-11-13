"""A failed dial through the pooled client must leave no live socket behind: the unraisable-teardown flake
family.
"""

import asyncio
import gc
import socket

import httpx
import pytest

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


def _closed_loopback_port() -> int:
    """A loopback port nothing listens on, reserved by the kernel then released.

    A hardcoded port is not a refused dial: whatever answers there decides the
    test's meaning, and a listener turns the dial into a success."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


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


async def _refused_dial_fetch() -> None:
    from crawlutron.transport.fetchers import default_http_fetcher

    # The REAL pooled-client dial, to a port nothing listens on. It must be
    # refused: a dial that connects leaves a socket for the wrong reason.
    port = _closed_loopback_port()
    with pytest.raises(httpx.ConnectError):
        await default_http_fetcher(canonical(f"http://127.0.0.1:{port}/"), {}, 5)


async def _refused_dial_fetch_then_reset() -> None:
    """Fetch, then reset INSIDE the fetch's own live loop -- the battery shape
    where the reset's close is scheduled onto the very loop that must run it."""
    await _refused_dial_fetch()
    await _reclaim_via_reset_seam()


async def test_a_failed_dial_leaves_no_connected_socket_after_the_reset_seam():
    """The red: after a refused dial, the socket survives the reset seam."""
    await _refused_dial_fetch()
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


async def test_a_socket_from_a_dead_loop_is_reclaimed_by_the_reset_seam():
    """The battery shape: the fetch's loop is gone when the close runs."""
    # A sibling loop (not the test's own) runs the fetch and dies; the close
    # must still reach its socket -- not decline and strand it for GC.
    await asyncio.to_thread(lambda: asyncio.run(_refused_dial_fetch()))
    await _reclaim_via_reset_seam()
    _assert_pool_released()
    gc.collect()
    leaked = _connected_dial_sockets()
    assert not leaked, f"dead-loop pooled client leaked sockets: {leaked}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-n0"]))
