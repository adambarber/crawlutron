"""The loop-lag probe must start on a running loop: ResourceGate is built at import
where no loop runs, so an ``__init__``-time start silently reads 0.0 forever --
startup is explicit once lifespan runs a loop.
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
    """The number load shedding keys off must be what the loop actually saw: a scripted
    clock advances ``interval + overshoot`` per pair, so lag is the overshoot PAST the
    interval and the interval itself never appears -- ``smoothing=1.0`` lets the newest sample win.
    """
    interval = 0.1
    step = interval + overshoot
    calls = itertools.count()

    # Each clock read advances the scripted time by ``step``, so the probe's
    # two reads per iteration span ``interval + overshoot``: lag reads as the overshoot.
    def scripted_clock() -> float:
        return next(calls) * step

    async def scripted_sleep(seconds: float) -> None:
        # Yield once; the scripted clock supplies the elapsed reading.
        await asyncio.sleep(0)
        del seconds

    probe = LoopLagProbe(interval=interval, smoothing=1.0, clock=scripted_clock, sleep=scripted_sleep)
    probe.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert probe.get() == pytest.approx(overshoot)
    finally:
        probe.stop()
