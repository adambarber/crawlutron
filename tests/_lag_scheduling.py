"""Shared loop-lag probe scripting helpers (#190): a scripted clock and sleep
let a probe report a chosen overshoot; ``lagged_gate`` wires them into a
ResourceGate whose lag probe is enabled for admission tests.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Awaitable, Callable

from app.runtime.pressure_sizing import ResourceGate


def lag_clock(lag: float, interval: float = 0.05) -> Callable[[], float]:
    """A clock that makes a probe observe exactly ``lag`` seconds of overshoot."""
    step = interval + lag
    calls = itertools.count()
    return lambda: next(calls) * step


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
