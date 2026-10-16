"""Zero-wall-clock instruments, so no test has to sleep to prove a timing claim:
a sleep is a bet on machine speed that the suite lost once, so each helper waits
on what the code asked for, and ``HANG_GUARD_SECONDS`` is the absolute, never-re-armed bound.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

HANG_GUARD_SECONDS = 5.0


async def await_signalled(
    event: asyncio.Event,
    what: str,
    *,
    guard: float = HANG_GUARD_SECONDS,
) -> None:
    """Await ``event``, failing with ``what`` if it never arrives; ``what`` names the
    hand-off being waited for, so a timeout says which observable never happened
    instead of leaving the failure unreadable.
    """
    try:
        await asyncio.wait_for(event.wait(), guard)
    except TimeoutError as exc:
        raise AssertionError(
            f"{what} never happened: the signal was not set within {guard}s. "
            "Nothing was waited on in wall-clock time, so this is a hang, not slowness."
        ) from exc


async def await_until(
    predicate: Callable[[], bool],
    what: str,
    *,
    guard: float = HANG_GUARD_SECONDS,
) -> None:
    """Yield the event loop until ``predicate()`` holds -- the honest replacement for
    ``await asyncio.sleep(0.05)  # let it run``: each iteration is a real scheduling
    point, so the loop exits on the condition, not a guess at how many turns it takes.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + guard
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError(
                f"{what} never became true within {guard}s of yielding the event loop. "
                "The task that should have set it never got far enough."
            )
        await asyncio.sleep(0)


class SleepRecorder:
    """Records every duration handed to ``asyncio.sleep`` while installed, still
    yielding exactly once so the interleaving is unchanged; asserting the *request*
    (0.3s whether granted in 0.3s or 3s) is what makes the claim reproducible.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._previous_sleep: Callable[..., Awaitable[None]] | None = None

    async def sleep(self, delay: float, *args: object, **kwargs: object) -> None:
        self.delays.append(delay)
        previous = self._previous_sleep
        assert previous is not None, "SleepRecorder.sleep used before install()"
        await previous(0)  # zero seconds, real yield

    def install(self, monkeypatch) -> SleepRecorder:
        """Patch ``asyncio.sleep`` for the duration of a test."""
        self._previous_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", self.sleep)
        return self

    def requests_over(self, seconds: float) -> list[float]:
        """Every recorded request of at least ``seconds``."""
        return [delay for delay in self.delays if delay >= seconds]

    def in_range(self, low: float, high: float) -> list[float]:
        """Every recorded request inside ``[low, high]``."""
        return [delay for delay in self.delays if low <= delay <= high]

    def count(self) -> int:
        return len(self.delays)


def recorder(monkeypatch) -> SleepRecorder:
    """Install and return a :class:`SleepRecorder` for this test."""
    return SleepRecorder().install(monkeypatch)


def cpu_seconds(work: Callable[[], object], *, samples: int = 3) -> float:
    """CPU seconds for the cheapest of ``samples`` runs of ``work`` -- the right
    instrument for a claim about *work* (linear, bounded), measuring ``process_time``
    not wall clock so a descheduled process cannot read as super-linear.
    """
    if samples < 1:
        raise ValueError(f"cpu_seconds needs at least one sample, got {samples}")
    work()
    started = time.process_time()
    work()
    best = time.process_time() - started
    for _ in range(samples - 1):
        started = time.process_time()
        work()
        best = min(best, time.process_time() - started)
    return best


__all__ = [
    "HANG_GUARD_SECONDS",
    "SleepRecorder",
    "await_signalled",
    "await_until",
    "cpu_seconds",
    "recorder",
]
