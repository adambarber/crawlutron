"""Concurrency observation as one capability, not per-test machinery: the hand-rolled racy form is replaced
by arrival/departure accounting and a deterministic rendezvous; a sequential regression fails on a bounded
wait -- a failure, never a hang.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import TypeVar

T = TypeVar("T")

__all__ = ["OverlapProbe"]


class OverlapProbe:
    """Wrap an async callable and measure its true concurrent peak: ``peak()`` is the widest number of calls
    in flight, recorded synchronously at arrival so no interleaving can hide overlap. With
    ``rendezvous_at=n`` each call parks (bounded) until n are in flight together.
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
        """Instrument ``answer``; hand the wrapped callable to the code under test. ``participate(args...)``
        gates which calls park at the rendezvous -- the seed fetch must not, since nothing can join it
        until it returns; all calls record the peak regardless.
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
