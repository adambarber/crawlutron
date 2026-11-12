"""Seam-parity (spec 9.6): old knob and kernel agree on the unsaturated band,
and assert the saturation delta (never a silent (0,0)). The kernel's ramp is
pre-warmed to capacity (the stationary regime) so a full grant is not paced.
"""

import asyncio

from admission import (
    AdmissionGate,
    AdmissionRefused,
    Bytes,
    Count,
    Duration,
    FloorDivisor,
    LaneId,
    Limits,
    Multiplier,
    PacingPolicy,
    Ratio,
    ResourceId,
    Sensing,
    SensorBinding,
    Want,
)
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import WeightedAdmission, kernel_lane_specs
from tests._clocks import kernel_clock
from tests.runtime._seam_sizing import seam_sizing_budget, seam_sizing_policy


class _FakeSignal:
    """A never-set event; a park here would hang, which the parity band must avoid."""

    async def wait(self, timeout=None):
        await asyncio.sleep(0)
        return False

    def set(self):
        return None


class _FakeGauge:
    """A kernel gauge pinned to one usage value, always warm and current."""

    def __init__(self, usage: float):
        self.usage = usage

    def is_warm(self) -> bool:
        return True

    def read_usage(self) -> Ratio:
        return Ratio(self.usage)

    def last_sample_age(self, clock_now: float) -> float:
        return 0.0


def _old_gate(mem_ratio, cpu_ratio) -> ResourceGate:
    return ResourceGate(memory_ratio=lambda: mem_ratio, cpu_ratio=lambda: cpu_ratio)


def _new_gate(mem_ratio, cpu_ratio) -> AdmissionGate:
    """The kernel gate for the same pressure, with the ramp pre-warmed to capacity
    (the stationary regime) so a full unsaturated grant is not paced below the want."""
    policy = seam_sizing_policy()
    lanes = kernel_lane_specs(policy, seam_sizing_budget())
    sensing = Sensing(
        {
            ResourceId.MEMORY: SensorBinding(
                Limits(target=Ratio(0.85), hard=Ratio(0.92)), _FakeGauge(mem_ratio)
            ),
            ResourceId.CPU: SensorBinding(
                Limits(target=Ratio(0.85), hard=Ratio(0.92)), _FakeGauge(cpu_ratio)
            ),
        }
    )
    # A 16x capacity ramp makes floor // lane_spread >= any realistic per-lane want.
    pacing = PacingPolicy(
        ramp_floor=Count(1),
        ramp_max=Count(4096),
        ramp_multiplier=Multiplier(1.10),
        floor_fraction=FloorDivisor(4),
        stale_bound=Duration(30.0),
        cold_probe=Count(1),
        initial_ramp=Count(4096),
    )
    _timeline, clock = kernel_clock()
    return AdmissionGate(
        pacing=pacing,
        lanes=lanes,
        sensing=sensing,
        clock=clock,
        signal=_FakeSignal(),
        memory_budget=Bytes(3 * 1024 * 1024 * 1024),
    )


async def _new_admit(gate, want_http, want_browser):
    async with gate.admit(Want.of(general=Count(want_http), heavy=Count(want_browser))) as granted:
        return granted.allocation.for_lane(LaneId.GENERAL).value, granted.allocation.for_lane(
            LaneId.HEAVY
        ).value


async def test_unsaturated_band_grants_identical_lane_decisions() -> None:
    """At equal observed pressure (usage under target) both doors grant the request
    byte-identically and land it in the same lane."""
    mem, cpu = 0.2, 0.2
    old = WeightedAdmission(_old_gate(mem, cpu), seam_sizing_policy())
    old_h, old_b = await old.admit(5, 2)
    assert old_h == 5 and old_b == 2

    new_h, new_b = await _new_admit(_new_gate(mem, cpu), 5, 2)
    assert (new_h, new_b) == (old_h, old_b)


async def test_saturation_band_never_silently_returns_zero() -> None:
    """At hard saturation the old (0,0)-yield becomes a kernel terminal (OVERSIZE
    refusal) or a floor grant, never a silent zero -- the declared delta (spec 9.6)."""
    mem, cpu = 0.99, 0.99
    old = WeightedAdmission(_old_gate(mem, cpu), seam_sizing_policy())
    old_h, old_b = await old.admit(5, 1)
    assert old_h == 0 and old_b == 0  # the legacy (0,0)-spin the kernel retires

    gate = _new_gate(mem, cpu)
    try:
        new_h, new_b = await _new_admit(gate, 5, 1)
    except AdmissionRefused:
        # OVERSIZE terminal: still not a silent (0,0); accepted delta escape.
        return
    # If it grants, the per-lane floor keeps a wanted lane at >= 1, never (0,0).
    assert new_h + new_b >= 1
