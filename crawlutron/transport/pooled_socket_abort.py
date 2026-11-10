"""Abort the OS sockets of pooled transports whose owning loop is dead.

A dead loop can never complete a transport close (the close needs the loop
to run its callbacks), so its transports sit in "closing" forever and warn
at GC -- the unraisable ``__del__`` flake family. The fd itself can still
be closed: it is loop-free.
"""

from __future__ import annotations

import asyncio.selector_events as selector_events
import gc
import socket
from typing import Protocol, cast

__all__ = ["abort_transport_sockets"]


class _AbortableTransport(Protocol):
    """The private transport surface the abort touches, declared as a Protocol
    rather than reached through the checker-invisible private class."""

    _closed: bool
    _sock: socket.socket | None


# The class itself is private asyncio surface, absent from the module's stubs:
# reached through its own __dict__ so no checker claims the attribute exists.
_selector_transport_cls: type = selector_events.__dict__["_SelectorSocketTransport"]


def abort_transport_sockets() -> int:
    """Close every live selector-transport socket regardless of loop state.

    Returns the count aborted. Only sockets with a peer are touched: a
    connectionless transport has nothing to leak.
    """
    aborted = 0
    for obj in gc.get_objects():
        if not isinstance(obj, _selector_transport_cls):
            continue
        transport = cast(_AbortableTransport, obj)
        sock = transport._sock
        if sock is None or sock.fileno() == -1:
            continue
        try:
            peer = sock.getpeername()
        except OSError:
            continue
        if not peer:
            continue
        transport._closed = True
        sock.close()
        # __del__ warns whenever ``_sock is not None`` (even a closed sock),
        # so dropping the reference is what silences the unraisable warning.
        transport._sock = None
        aborted += 1
    return aborted
