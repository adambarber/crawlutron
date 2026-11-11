"""Abort the OS sockets of pooled transports whose owning loop is dead.

A dead loop can never complete a transport close (the close needs the loop
to run its callbacks), so its transports sit in "closing" forever and warn
at GC -- the unraisable ``__del__`` flake family. The fd itself can still
be closed: it is loop-free.
"""

from __future__ import annotations

import asyncio
import socket
from typing import Protocol, cast

__all__ = ["abort_transport_sockets"]


class _AbortableTransport(Protocol):
    """The private transport surface the abort touches, declared as a Protocol
    rather than reached through the checker-invisible private class."""

    _closed: bool
    _sock: socket.socket | None


def _abort_transport(transport: object) -> int:
    """Close one live transport socket loop-free; 1 if aborted, 0 if nothing to do."""
    t = cast(_AbortableTransport, transport)
    sock = t._sock
    if sock is None or sock.fileno() == -1:
        return 0
    try:
        peer = sock.getpeername()
    except OSError:
        return 0
    if not peer:
        return 0
    t._closed = True
    sock.close()
    # __del__ warns whenever ``_sock is not None`` (even a closed sock),
    # so dropping the reference is what silences the unraisable warning.
    t._sock = None
    return 1


def _transports_of(dead_loop: asyncio.AbstractEventLoop) -> list[object]:
    """The transports a loop still tracks (its own registry, not a heap scan).

    A dead loop that was never ``close()``d keeps ``_transports``, the
    WeakValueDictionary asyncio populates per transport; a loop with no such
    registry simply yields nothing.
    """
    registry = getattr(dead_loop, "_transports", None)
    if registry is None:
        return []
    try:
        return [t for t in list(registry.values()) if t is not None]
    except Exception:
        # A partially-finalized loop can refuse enumeration; there is then
        # nothing tracked left to abort.
        return []


def abort_transport_sockets(dead_loops: tuple[asyncio.AbstractEventLoop, ...] = ()) -> int:
    """Close every live selector-transport socket of the given dead loops.

    Returns the count aborted. Only sockets with a peer are touched: a
    connectionless transport has nothing to leak. Callers name the dead loop
    they are resetting behind, so the walk is O(that loop's transports) —
    never O(heap).
    """
    aborted = 0
    seen: set[int] = set()
    for dead_loop in dead_loops:
        for transport in _transports_of(dead_loop):
            if id(transport) in seen:
                continue
            seen.add(id(transport))
            aborted += _abort_transport(transport)
    return aborted
