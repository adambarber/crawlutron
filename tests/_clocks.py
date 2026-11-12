"""The suite's one clock home (#843): every clock-like symbol lives here. Arrangement clocks construct
stale/future state; instruments prove timing claims with zero wall clock; a clock-like def elsewhere in
tests/** reds at the tests-define-no-local-clocks invariant.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from collections.abc import Awaitable, Callable

from admission import Instant

from crawlutron.models.json_value import JsonValue
from crawlutron.runtime.pressure_sizing import ResourceGate
from crawlutron.transport.content_cache import CONTENT_CACHE_MAX_TOTAL_BYTES, ContentCache

#: The type of an injectable monotonic-shaped clock.
Clock = Callable[[], float]

#: Where every clock-like DEF in tests/** must live (the invariant's read).
CLOCK_HOME = "tests/_clocks.py"

ClockState = dict[str, float]

HANG_GUARD_SECONDS = 5.0

#: Captured at import, before any test's runtime patch: an advance-on-sleep
#: helper must yield through the real primitive, never re-enter its own patch.
_REAL_ASYNCIO_SLEEP = asyncio.sleep


def arrange_clock(start: float = 1000.0) -> tuple[ClockState, Clock]:
    """A wall clock the test owns and advances: state handle + monotonic clock.

    For constructing stale/future state (TTL expiry, cooldown windows) -- never
    for asserting on elapsed wall time; that is what ``wall_assertion`` marks.
    """
    state: ClockState = {"now": start}

    def clock() -> float:
        return state["now"]

    return state, clock


def advance_clock(state: ClockState, seconds: float) -> None:
    """Move an :func:`arrange_clock` state forward (the one mutation verb)."""
    state["now"] += seconds


def lag_clock(lag: float, interval: float = 0.05) -> Clock:
    """A clock that makes a probe observe exactly ``lag`` seconds of overshoot."""
    step = interval + lag
    calls = itertools.count()
    return lambda: next(calls) * step


def kernel_clock(start: float = 0.0):
    """An Instant-returning clock over a movable float timeline (kernel gate).
    The kernel gate's :class:`~admission.Clock` needs ``now() -> Instant``; this
    adapts the suite's one movable timeline into that typed boundary."""
    timeline = Timeline(start)

    class _Now:
        def now(self):
            return Instant._of(timeline())

    return timeline, _Now()


async def scripted_sleep(seconds: float) -> None:
    await asyncio.sleep(0)
    del seconds


def lagged_gate(
    lag: float = 0.30,
    *,
    sleep: Callable[[float], Awaitable[None]] = scripted_sleep,
) -> ResourceGate:
    """A gate with an enabled, clock-scripted loop-lag probe for admission tests."""
    return ResourceGate(
        memory_ratio=lambda: 0.0,
        cpu_ratio=lambda: 0.0,
        lag_probe_enabled=True,
        lag_probe_clock=lag_clock(lag),
        lag_probe_sleep=sleep,
    )


class Timeline:
    """A mutable monotonic clock the test advances (or that advances on sleep)."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.requested: list[float] = []

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def advance_on_sleep(timeline: Timeline, requested: list[float], delay: float | None = None) -> None:
    """Record the sleep request and move the clock past it: parks and recoveries
    elapse deterministically, zero wall clock."""
    if delay:
        requested.append(delay)
        timeline.now += delay
    await _REAL_ASYNCIO_SLEEP(0)


def parking_clock(monkeypatch, timeline: Timeline | None = None) -> tuple[Timeline, list[float]]:
    """Patch the scheduler's sleep to advance ``timeline`` by every request:
    parks elapse deterministically, zero wall clock."""
    timeline = timeline or Timeline()
    requested = timeline.requested
    monkeypatch.setattr(
        "crawlutron.transport.scheduler.asyncio.sleep",
        lambda delay=None: advance_on_sleep(timeline, requested, delay),
    )
    return timeline, requested


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


def clocked_cache(
    directory: str,
    *,
    default_ttl: float = 1,
    clock_start: float = 1000.0,
    max_entries: int = 10000,
    max_total_bytes: int | None = CONTENT_CACHE_MAX_TOTAL_BYTES,
) -> tuple[ContentCache, list[float]]:
    """A cache over ``directory`` reading time from a handle the caller advances.

    Expiry is the subject of most of these tests, so a movable clock handle is
    returned beside the cache -- no site declares a list and a lambda over it."""
    clock = [clock_start]
    return (
        ContentCache(
            directory,
            default_ttl=default_ttl,
            clock=lambda: clock[0],
            max_entries=max_entries,
            max_total_bytes=max_total_bytes,
        ),
        clock,
    )


def make_seeded_cache(
    directory: str,
    *keys: str,
    value: JsonValue | None = None,
    default_ttl: float = 1,
    max_entries: int = 10000,
    max_total_bytes: int | None = CONTENT_CACHE_MAX_TOTAL_BYTES,
) -> tuple[ContentCache, list[float]]:
    """:func:`clocked_cache` holding one entry per key.

    Seeding + a clock are the two halves of "an entry that expires"; a test
    needing both used to write down directory, TTL, clock list and entry."""
    cache, clock = clocked_cache(
        directory, default_ttl=default_ttl, max_entries=max_entries, max_total_bytes=max_total_bytes
    )
    for key in keys:
        cache.set(key, {"data": "1"} if value is None else value)
    return cache, clock


__all__ = [
    "CLOCK_HOME",
    "Clock",
    "ClockState",
    "HANG_GUARD_SECONDS",
    "SleepRecorder",
    "Timeline",
    "advance_clock",
    "advance_on_sleep",
    "arrange_clock",
    "await_signalled",
    "await_until",
    "clocked_cache",
    "cpu_seconds",
    "kernel_clock",
    "lag_clock",
    "lagged_gate",
    "make_seeded_cache",
    "parking_clock",
    "recorder",
    "scripted_sleep",
]
