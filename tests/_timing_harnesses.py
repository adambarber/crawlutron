"""Zero-wall-clock instruments, so no test has to sleep to prove something.

Every helper here follows one rule: an assertion about *timing* is only
reproducible if it is really an assertion about something else. A sleep is not
evidence, it is a bet that the machine is fast enough -- and a bet the suite
lost, once, to a box running four agent sessions (62.9s of user CPU inside a
271.5s full run). These helpers let a test name the thing it is actually
waiting for -- a signal, an ordering, a duration the code *asked for* -- and
wait on exactly that.

Three shapes, none of which wait for wall-clock seconds:

* :class:`SleepRecorder` -- assert on the durations the code requested, not on
  how long the box took to grant them. A scheduler that asked to sleep 0.3s has
  honoured a 0.3s cooldown whether that took 0.3s or 3s.
* :func:`await_signalled` -- await an ``asyncio.Event`` that the thing under
  test sets. This is the handshake primitive: it completes only if the code
  under test actually did the thing.
* :func:`await_until` -- yield the loop until a named predicate holds. This is
  the "let the already-scheduled task run" primitive, minus the optimism: it
  does not sleep a hopeful 50ms and hope, it runs until the condition is true.

``HANG_GUARD_SECONDS`` is the one wall-clock number here, and it is deliberately
NOT an instrument. An event-driven wait returns the instant its event fires, no
matter how long ago that was; the guard only catches a coroutine that will
*never* receive its event, and would otherwise hang the whole run instead of
failing one test. It is re-armed by every observed signal, so it measures
silence rather than slowness, and a correct wait returns in microseconds.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable

# Bound on how long a wait may observe NOTHING before it calls the test a hang.
# Measured, not guessed: the slowest edge-driven wait observed across a full
# top-level `pytest -q` sweep is well under a second on this box, and the one
# real starvation incident produced a job that was still progressing -- it
# simply had not been scheduled yet, which is the case an event-driven wait
# now rides out instead of racing.
HANG_GUARD_SECONDS = 5.0


async def await_signalled(
    event: asyncio.Event,
    what: str,
    *,
    guard: float = HANG_GUARD_SECONDS,
) -> None:
    """Await ``event``, failing the test with ``what`` if it never arrives.

    ``what`` is the name of the hand-off being waited for ("the eager close of
    the superseded client"). Naming it is the point: a wait that times out has
    to say which observable never happened, or the failure is unreadable.
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
    """Yield the event loop until ``predicate()`` holds.

    The honest replacement for ``await asyncio.sleep(0.05)  # let it run``.
    Each iteration is a real scheduling point (the suite's autouse sleep patch
    makes ``asyncio.sleep`` a zero-second sleep that still yields), so any
    already-scheduled task gets its turn; the loop exits on the condition
    rather than on a guess about how many turns it needed.
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
    """Records every duration handed to ``asyncio.sleep`` while it is installed.

    The replacement still yields exactly once, so the interleaving the code
    under test depends on is unchanged -- only the wall-clock cost is dropped.
    What is captured is the *request*: a 0.3s cooldown is a 0.3s request
    whether the machine grants it in 0.3s or in 3s, and asserting the request
    is what makes the test reproducible on a loaded box.
    """

    def __init__(self) -> None:
        self.delays: list[float] = []
        self._previous_sleep: Callable[..., object] | None = None

    async def sleep(self, delay: float, *args: object, **kwargs: object) -> None:
        self.delays.append(delay)
        previous = self._previous_sleep
        assert previous is not None, "SleepRecorder.sleep used before install()"
        await previous(0)  # type: ignore[operator]  # zero seconds, real yield

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


__all__ = [
    "HANG_GUARD_SECONDS",
    "SleepRecorder",
    "await_signalled",
    "await_until",
    "recorder",
]
