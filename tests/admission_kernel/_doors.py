"""Shared arrange helpers for kernel acceptance tests.

Lets a test acquire a live grant and exit it deliberately instead of
auto-releasing inside the ``with`` body.
"""

from __future__ import annotations

from admission import Allocation, Count, Want

_GENERAL_ONE = Want.of(general=Count(1), heavy=Count(0))


async def _live(gate, want: Want):
    """Enter the gate and return ``(cm, granted)`` with the lease still live."""
    cm = gate.admit(want)
    granted = await cm.__aenter__()
    return cm, granted


async def _finish(cm) -> None:
    """Exit the context manager, releasing the lease cleanly."""
    await cm.__aexit__(None, None, None)


async def _auto(gate, want: Want) -> Allocation:
    """Grant and auto-release on body exit: reads the allocation only."""
    async with gate.admit(want) as granted:
        return granted.allocation


_GENERAL_ONE = Want.of(general=Count(1), heavy=Count(0))


async def _hold_one(gate):
    """A live single-GENERAL grant and its still-open context manager."""
    return await _live(gate, _GENERAL_ONE)


__all__ = ["_GENERAL_ONE", "_auto", "_finish", "_hold_one", "_live"]
