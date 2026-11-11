"""Concurrency observation as one capability, not per-test machinery.

Every test that must prove "these fetches ran concurrently" used to hand-roll
the same racy observation: a shared set, a widest counter, and a sleep as the
delivery mechanism — the sleep-race flake family (#829, #830, and the
recursive-concurrency sighting). OverlapProbe replaces all of it: the widest
in-flight point is recorded by arrival/departure accounting that never depends
on scheduler timing, and `rendezvous_at` deterministically holds fetchers
until n are in flight together, so the peak assertion bites without any sleep
and a sequential regression fails on an expired bounded wait — a failure,
never a hang (the 120s global law).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

__all__ = ["OverlapProbe"]


class OverlapProbe:
    """Wrap an async callable and measure its true concurrent peak.

    `peak()` returns the widest number of calls in flight at any moment —
    recorded synchronously at arrival, so no scheduler interleaving can hide
    overlap. With `rendezvous_at=n`, each wrapped call parks (bounded) until n
    calls are in flight together; the fetch still completes honestly either
    way, so a sequential regression surfaces as a peak-assert failure, not an
    error.
    """

    def __init__(self, *, rendezvous_at: int = 0, rendezvous_seconds: float = 0.5) -> None:
        self._live: set[int] = set()
        self._widest = 0
        self._gate = asyncio.Event()
        self._rendezvous_at = rendezvous_at
        self._rendezvous_seconds = rendezvous_seconds

    def wrap(
        self,
        answer: Callable[..., Awaitable[T]],
        *,
        participate: Callable[..., bool] | None = None,
    ) -> Callable[..., Awaitable[T]]:
        """Instrument ``answer``; hand the wrapped callable to the code under test.

        ``participate(args...)`` gates which calls park at the rendezvous (e.g.
        the seed fetch must not: nothing can join it until it returns). All
        calls record the peak regardless.
        """

        async def observed(*args: object, **kwargs: object) -> T:
            key = id(asyncio.current_task())
            self._live.add(key)
            self._widest = max(self._widest, len(self._live))
            try:
                if self._rendezvous_at and len(self._live) >= self._rendezvous_at:
                    self._gate.set()
                if self._rendezvous_at and (participate is None or participate(*args, **kwargs)):
                    # Park until the cohort is in flight together; a sequential
                    # schedule never gathers it and the peak assert fails.
                    try:
                        await asyncio.wait_for(self._gate.wait(), timeout=self._rendezvous_seconds)
                    except TimeoutError:
                        pass
                return await answer(*args, **kwargs)
            finally:
                self._live.discard(key)

        return observed

    def peak(self) -> int:
        """The widest in-flight count observed so far."""
        return self._widest
