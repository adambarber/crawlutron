"""PARK must not wait forever — it re-checks under the stale bound.

Row 8: a parked admit whose signal never fires re-checks under the stale bound
instead of blocking forever on the edge signal.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from crawlutron_admit import (
    AdmissionRefused,
    Bytes,
    Cost,
    Count,
    Duration,
    FloorDivisor,
    IdleReason,
    LaneId,
    LaneSpec,
    Limits,
    MinAll,
    Multiplier,
    PacingPolicy,
    Ratio,
    ResourceId,
    Sensing,
    SensorBinding,
)
from crawlutron_admit.gate import AdmissionGate
from crawlutron_admit.values import Instant
from tests._support._doors import _GENERAL_ONE, _ceiling_one_gate, _live
from tests._support._gate_factory import FakeGauge, FakeSignal


class _TransitioningGauge:
    """Warm/low-usage for ~300 ms, then stale forever.

    ``is_warm`` drops after 300 ms so the gate's stale-detection logic
    transitions the CPU resource from PARK → STALE.
    """

    __slots__ = ("_usage", "_anchor")

    def __init__(self, usage: float) -> None:
        self._usage = usage
        self._anchor = time.monotonic()

    def is_warm(self) -> bool:
        return (time.monotonic() - self._anchor) < 0.3

    def read_usage(self) -> Ratio:
        return Ratio(self._usage)

    def last_sample_age(self, now: float) -> float:
        elapsed = time.monotonic() - self._anchor
        return float("inf") if elapsed > 0.3 else 0.0


def _build_parked_gate(stale_bound: float = 0.5) -> tuple:
    """Gate with GENERAL ceiling 1, warm gauges that transition stale."""
    mib = 1024 * 1024
    fake_signal = FakeSignal()

    class _RealClock:
        def now(self) -> Instant:
            return Instant.of(time.monotonic())

    sensing = Sensing(
        {
            ResourceId.MEMORY: SensorBinding(
                Limits(target=Ratio(0.6), hard=Ratio(0.9)),
                FakeGauge(0.2, warm=True),
            ),
            ResourceId.CPU: SensorBinding(
                Limits(target=Ratio(0.6), hard=Ratio(0.9)),
                _TransitioningGauge(0.4),
            ),
        }
    )
    lanes = {
        LaneId.GENERAL: LaneSpec(
            ceiling=Count(1),
            cost=Cost(Bytes(1 * mib)),
            scale_source=MinAll(),
        ),
        LaneId.HEAVY: LaneSpec(
            ceiling=Count(0),
            cost=Cost(Bytes(700 * mib)),
            scale_source=MinAll(),
        ),
    }
    pacing = PacingPolicy(
        ramp_floor=Count(1),
        ramp_max=Count(64),
        ramp_multiplier=Multiplier(1.10),
        floor_fraction=FloorDivisor(4),
        stale_bound=Duration(stale_bound),
        cold_probe=Count(1),
        initial_ramp=None,
        ramp_growth_window=Count(3),
    )

    gate = AdmissionGate(
        pacing=pacing,
        lanes=lanes,
        sensing=sensing,
        clock=_RealClock(),
        signal=fake_signal,
    )
    return gate, fake_signal


async def test_park_without_signal_re_checks_and_raises_stale() -> None:
    """A signal-less park must not hang: the bounded wait re-decides, and the
    gauge turning stale makes that re-decision terminal (STALE). On HEAD the
    park blocks on the signal forever, so this fails by timeout.
    """
    gate, _ = _build_parked_gate(stale_bound=0.5)

    start_park = asyncio.Event()

    async def _parker():
        await start_park.wait()
        return await _live(gate, _GENERAL_ONE)

    # Parker created FIRST so its _lease_ctx contextvar is False.
    parker = asyncio.create_task(_parker())
    # Fill the sole slot in THIS task — holder context sets _lease_ctx=True.
    cm_holder, granted_holder = await _live(gate, _GENERAL_ONE)

    start_park.set()
    await asyncio.sleep(0)  # let the parker decide PARK and hit the signal

    # The signal never fires; the gauge goes stale after ~300 ms, so the bound
    # (0.5 s) re-checks and refuses. Unbounded, the park never returns.
    with pytest.raises(AdmissionRefused) as exc_info:
        async with await asyncio.wait_for(parker, timeout=5.0):
            pass

    assert exc_info.value.reason is IdleReason.STALE
    granted_holder.lease.release(work_completed=True)
    await cm_holder.__aexit__(None, None, None)


async def test_park_woken_by_release_proceeds_promptly() -> None:
    """A release sets the signal and wakes the parker before the bound elapses."""
    gate, handles = _ceiling_one_gate()

    start_park = asyncio.Event()

    async def _parker():
        await start_park.wait()
        return await _live(gate, _GENERAL_ONE)

    # Parker task created BEFORE holder enters so its contextvar sees
    # _lease_ctx=False (admit context is task-isolated).
    parker = asyncio.create_task(_parker())
    cm_a, granted_a = await _live(gate, _GENERAL_ONE)

    start_park.set()
    await asyncio.sleep(0)  # let the parker hit the parked signal wait

    # Release: sets the signal (and the latch).  Parker should wake promptly
    # and grant instead of waiting for the stale bound to elapse.
    granted_a.lease.release(work_completed=True)
    await cm_a.__aexit__(None, None, None)

    cm_parker, granted_parker = await asyncio.wait_for(parker, timeout=5.0)
    # Verify the allocation is genuine (not a refusal).
    assert granted_parker.allocation.total.value >= 1
    await cm_parker.__aexit__(None, None, None)
