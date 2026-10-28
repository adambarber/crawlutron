"""A liveness probe shared by the event-loop discipline tests.

Declared support surface (#476): moved verbatim from
test_event_loop_non_blocking.py so a sibling test module imports it from here,
not from another test module's internals.
"""

from __future__ import annotations

import asyncio
import threading

from tests._timing_harnesses import HANG_GUARD_SECONDS

__all__ = ["LoopLiveness"]


class LoopLiveness:
    """A counter only the event loop can advance.

    The heartbeat runs on the loop and bumps the count under a ``Condition``;
    the blocking work waits on it and reports how far the loop got."""

    def __init__(self, needed: int = 3) -> None:
        self.needed = needed
        self.ticks = 0
        self._condition = threading.Condition()

    async def heartbeat(self) -> None:
        while True:
            await asyncio.sleep(0)
            with self._condition:
                self.ticks += 1
                self._condition.notify_all()

    def wait_for_progress(self, timeout: float = HANG_GUARD_SECONDS) -> int:
        """Blocking worker: return how far the loop advanced while we waited.

        The baseline is snapshotted on entry, so only ticks landing WHILE the
        blocking work is in flight can satisfy the count."""
        with self._condition:
            baseline = self.ticks
            self._condition.wait_for(lambda: self.ticks - baseline >= self.needed, timeout=timeout)
            return self.ticks - baseline

    async def run_with_heartbeat(self, work, *, timeout: float):
        """Await ``work()`` with the heartbeat ticking; cancel the tick task.

        The shared scaffolding -- spawn, bound the run, cancel no matter how the
        work exits -- lives here so a test cannot leak a heartbeat."""
        hb = asyncio.ensure_future(self.heartbeat())
        try:
            return await asyncio.wait_for(work(), timeout=timeout)
        finally:
            hb.cancel()
