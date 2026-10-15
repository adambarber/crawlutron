"""The loop-lag probe must start on a running loop.

ResourceGate is constructed at import time (engine module level) where no
event loop runs, so an __init__-time probe start silently never schedules
and lag reads 0.0 forever. Startup is explicit: lifespan calls
start_lag_probe() once a loop runs; construction alone never starts it.
"""

import asyncio
import itertools

import pytest

import app.runtime.pressure_probes as rp
from app.runtime.resource_pressure import LoopLagProbe, ResourceGate


def test_probe_does_not_start_without_running_loop():
    probe = LoopLagProbe()
    probe.start()
    assert probe._task is None
    assert probe.get() == 0.0


def test_loop_lag_probe_flag_accepts_shared_bool_spellings(monkeypatch):
    import importlib

    for value in (" true ", "yes", "1"):
        monkeypatch.setenv("CRAWLUTRON_LOOP_LAG_PROBE", value)
        module = importlib.reload(rp)
        assert module.LOOP_LAG_PROBE_ENABLED is True
    monkeypatch.delenv("CRAWLUTRON_LOOP_LAG_PROBE")
    importlib.reload(rp)


def test_gate_construction_never_starts_probe():
    gate = ResourceGate(lag_probe_enabled=True)
    assert gate._lag_probe._task is None


async def test_gate_probe_starts_explicitly_on_running_loop():
    gate = ResourceGate(lag_probe_enabled=True)
    gate.start_lag_probe()
    try:
        task = gate._lag_probe._task
        assert task is not None and not task.done()
        # Idempotent: a second start reuses the running task.
        gate.start_lag_probe()
        assert gate._lag_probe._task is task
    finally:
        gate.close()
    assert gate._lag_probe._task is None


@pytest.mark.parametrize("overshoot", [0.0, 0.25, 0.9])
async def test_probe_reports_the_overshoot_it_observed(monkeypatch, overshoot):
    """The number load shedding keys off has to be the one the loop actually saw.

    The tests above assert task bookkeeping -- created, not done, idempotent,
    cleared on close -- and hold whether or not the measurement is correct.
    The probe exists to notice a busy loop, so this drives the sampling loop
    with a scripted clock and checks what comes out.

    Each iteration reads the clock twice: once for ``t0``, once after the sleep.
    Advancing by ``interval + overshoot`` per pair is what a loop that overran
    its sleep by exactly ``overshoot`` looks like, so the expected reading is
    that overshoot and nothing else. ``smoothing=1.0`` makes the newest sample
    win outright, so the assertion is about the measurement rather than about
    an exponential moving average's convergence, and the value settles no
    matter how many iterations run.

    The clock is injected rather than slept against on purpose: the suite
    patches ``time.sleep`` and ``asyncio.sleep`` to zero across every test, so
    a real stall here would silently measure nothing.

    ``overshoot=0.0`` carries the healthy case in the same parameter set, which
    is why it is here rather than in a test of its own: lag is the overshoot
    PAST the interval, not the elapsed time, and a probe that charged a loop
    the full interval every sample would report a permanently stalled service
    and pin the gate at its hardest setting. A loop that keeps to time advances
    by exactly ``interval``, so the expected reading is 0.0 and the interval
    itself never appears as a result. Reading 0.0 out of a 0.1 interval is the
    only evidence that the difference is being measured, and splitting the zero
    case into a separate function duplicated every line but the arithmetic.
    """
    interval = 0.1
    step = interval + overshoot
    calls = itertools.count()
    monkeypatch.setattr(rp.time, "monotonic", lambda: (next(calls) // 2) * step)

    probe = LoopLagProbe(interval=interval, smoothing=1.0)
    probe.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert probe.get() == pytest.approx(overshoot)
    finally:
        probe.stop()
