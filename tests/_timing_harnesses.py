"""Zero-wall-clock instruments, so no test has to sleep to prove something.

Every helper here follows one rule: an assertion about *timing* is only
reproducible if it is really an assertion about something else. A sleep is not
evidence, it is a bet that the machine is fast enough -- and a bet the suite
lost, once, to a box running four agent sessions (62.9s of user CPU inside a
271.5s full run). These helpers let a test name the thing it is actually
waiting for -- a signal, an ordering, a duration the code *asked for* -- and
wait on exactly that.

Four shapes, none of which wait for wall-clock seconds:

* :class:`SleepRecorder` -- assert on the durations the code requested, not on
  how long the box took to grant them. A scheduler that asked to sleep 0.3s has
  honoured a 0.3s cooldown whether that took 0.3s or 3s.
* :func:`await_signalled` -- await an ``asyncio.Event`` that the thing under
  test sets. This is the handshake primitive: it completes only if the code
  under test actually did the thing.
* :func:`await_until` -- yield the loop until a named predicate holds. This is
  the "let the already-scheduled task run" primitive, minus the optimism: it
  does not sleep a hopeful 50ms and hope, it runs until the condition is true.
* :func:`cpu_seconds` -- assert on how much *work* a call cost, not on how long
  the box took to let it happen. For claims about algorithmic cost only; a test
  whose subject is a deadline the code asked for wants the wall clock instead.

``HANG_GUARD_SECONDS`` is the one wall-clock number here, and it is deliberately
NOT an instrument. An event-driven wait returns the instant its event fires, no
matter how long ago that was, so a correct wait costs microseconds and the guard
only ever fires on a wait that cannot complete -- a coroutine that will never
receive its event, which would otherwise hang the whole run rather than failing
one test.

It is an absolute bound on the whole wait, not a silence meter: it is set once
and is NOT re-armed as the wait observes progress. So a wait that trickles
progress for longer than the guard still fails. That is the stricter contract,
and the deliberate one -- a suite that rides out arbitrarily slow progress is a
suite that has stopped noticing a stall. The job-store harness in
``_job_harnesses`` takes the other trade for its own waits, where a live runner
task exists to be joined instead of a deadline to be met.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable

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
    """CPU seconds for the cheapest of ``samples`` runs of ``work``.

    The instrument for a claim about *work* -- "this is linear", "this is
    bounded" -- and the wrong one for everything else.

    ``time.perf_counter`` measures how long the box took, which is a claim
    about the machine as much as about the code: a process that is descheduled
    keeps the wall clock running while its own CPU clock stands still. On a
    loaded runner a linear implementation therefore measures as super-linear,
    which is how ``test_strip_is_linear_under_pathological_unclosed_script_flood``
    came to report a 37x blowup for a 2x input against a scanner that spends a
    flat ~3.9ns per byte from 50KB to 1.6MB. ``process_time`` excludes the
    deschedule and keeps the quadratic, because a quadratic implementation
    really does burn four times the CPU.

    ``samples`` and the min-of-N are for the same reason the ``SleepRecorder``
    exists: the cheapest run is the least contaminated one, and first-call
    effects (lazy compiles, imports, cache fills) are settled by the untimed
    warm-up rather than by sleeping.
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
