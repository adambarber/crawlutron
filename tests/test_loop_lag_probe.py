"""The loop-lag probe must start on a running loop: ResourceGate is built at import
where no loop runs, so an ``__init__``-time start silently reads 0.0 forever --
startup is explicit once lifespan runs a loop.
"""

import asyncio

import pytest

from crawlutron.runtime.pressure_sizing import LoopLagProbe
from tests._clocks import await_until
from tests._lag_scheduling import lag_clock, lagged_gate, scripted_sleep


def test_probe_does_not_start_without_running_loop():
    # A scripted laggy clock would report 0.30 of lag IF a sampler ever ran; on a
    # no-loop ``start()`` it never does, so the baseline stays untouched.
    probe = LoopLagProbe(interval=0.05, smoothing=1.0, clock=lag_clock(0.30), sleep=scripted_sleep)
    probe.start()
    assert probe.get() == 0.0


def test_gate_construction_never_starts_probe():
    # An enabled probe is built but not started by the constructor; even with a
    # delay clock that would throttle once sampling, no lag is applied.
    gate = lagged_gate(lag=0.30)
    assert gate.batch_scale_factor() == 1.0


async def test_gate_probe_starts_explicitly_on_running_loop():
    gate = lagged_gate(lag=0.30)
    # Not started at construction: no throttle yet.
    assert gate.batch_scale_factor() == 1.0
    gate.start_lag_probe()
    try:
        for _ in range(30):
            await asyncio.sleep(0)
        # Idempotent: a second start reuses the running sampler, so the factor
        # keeps reflecting one lag reading (0.30 -> full throttle), not doubled.
        gate.start_lag_probe()
        for _ in range(30):
            await asyncio.sleep(0)
        assert gate.batch_scale_factor() == pytest.approx(0.0, abs=0.01)
    finally:
        gate.close()
    # After close() the sampler stops: further would-be samples never change the
    # throttle -- the scale is frozen at its last settled value.
    settled = gate.batch_scale_factor()
    for _ in range(30):
        await asyncio.sleep(0)
    assert gate.batch_scale_factor() == settled


@pytest.mark.parametrize("overshoot", [0.0, 0.25, 0.9])
async def test_probe_reports_the_overshoot_it_observed(monkeypatch, overshoot):
    """The number load shedding keys off must be what the loop actually saw: a scripted
    clock advances ``interval + overshoot`` per pair, so lag is the overshoot PAST the
    interval and the interval itself never appears -- ``smoothing=1.0`` lets the newest sample win.
    """
    interval = 0.1
    probe = LoopLagProbe(
        interval=interval,
        smoothing=1.0,
        clock=lag_clock(overshoot, interval=interval),
        sleep=scripted_sleep,
    )
    probe.start()
    try:
        for _ in range(20):
            await asyncio.sleep(0)
        assert probe.get() == pytest.approx(overshoot)
    finally:
        probe.stop()


async def test_a_raising_tick_retains_last_good_lag_and_keeps_the_probe_alive():
    """A sensor fault must never read as full health: the probe retains its last good
    lag, counts the fault, and keeps sampling so no dead sensor emits a fake healthy 0.0.
    """
    interval = 0.05
    sleep_calls = [0]
    raised = [False]

    async def raising_sleep(seconds):
        sleep_calls[0] += 1
        del seconds
        if sleep_calls[0] == 2:
            raised[0] = True
            raise RuntimeError("event loop closed")
        await asyncio.sleep(0)

    probe = LoopLagProbe(
        interval=interval,
        smoothing=1.0,
        clock=lag_clock(0.20, interval=interval),
        sleep=raising_sleep,
    )
    probe.start()
    try:
        await await_until(lambda: raised[0], "the raising tick fires")
        # Tick 1 established a genuine 0.20s overshoot; the fault must not reset it.
        assert probe.get() == pytest.approx(0.20, abs=0.01)
        assert probe.get() != 0.0
        assert probe.factor() != 1.0
    finally:
        probe.stop()


async def test_repeated_raising_ticks_surface_a_fault_count():
    """The operator sees a struggling sensor, never a fake healthy zero: every raising
    tick after the first still increments a readable counter while last-good lag holds.
    """
    sleep_calls = [0]

    async def raising_sleep(seconds):
        sleep_calls[0] += 1
        del seconds
        if 1 <= sleep_calls[0] <= 3:
            raise RuntimeError("loop kept failing")
        await asyncio.sleep(0)

    probe = LoopLagProbe(
        interval=0.05,
        smoothing=1.0,
        clock=lag_clock(0.20, interval=0.05),
        sleep=raising_sleep,
    )
    probe.start()
    try:
        await await_until(lambda: probe.faults() == 3, "three raising ticks are counted")
        assert probe.faults() == 3
        # The probe survives the fault storm and keeps sampling: a later healthy
        # tick lands and the last-good lag is now a real 0.20 measuring.
        await await_until(
            lambda: probe.get() == pytest.approx(0.20, abs=0.01), "a post-fault healthy tick lands"
        )
    finally:
        probe.stop()
