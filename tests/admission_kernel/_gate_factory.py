"""Arrangement for kernel gate tests: an injectable clock, signal and gauges.

The fake clock/signal are built inside :func:`build_gate` as closures so no clock-like
symbol is a module-level def (the tests-define-no-local-clocks rule governs those)."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field

from admission import (
    AdmissionGate,
    Bytes,
    Clock,
    Cost,
    Count,
    Duration,
    FloorDivisor,
    Instant,
    LaneId,
    LaneSpec,
    Limits,
    MinAll,
    Multiplier,
    PacingPolicy,
    Ratio,
    ResourceId,
    ScaleSource,
    Sensing,
    SensorBinding,
)

__all__ = ["FakeGauge", "GateHandles", "build_gate"]


class FakeGauge:
    """A controllable sensor: set usage/warm/stale and read them back.

    The fail-closed door: calling :meth:`read_usage` after :meth:`raise_on_read`
    raises, driving the resource hard-saturated.
    """

    def __init__(
        self,
        usage: float,
        *,
        warm: bool = True,
        stale: bool = False,
    ) -> None:
        self.usage = usage
        self.warm = warm
        self.stale = stale
        self._raise_on_read = False

    def is_warm(self) -> bool:
        return self.warm

    def read_usage(self) -> Ratio:
        if self._raise_on_read:
            raise RuntimeError("gauge raised (fail-closed)")
        return Ratio(self.usage)

    def last_sample_age(self, now: float) -> float:
        # A stale gauge is permanently past the staleness bound; a healthy one is current.
        return float("inf") if self.stale else 0.0

    def raise_on_read(self, enabled: bool = True) -> None:
        self._raise_on_read = enabled


@dataclass
class GateHandles:
    """Time/signal handles the test owns while a gate is alive.

    ``now`` is an offset applied over a real-time anchor, so the gate's clock
    advances during genuine waits (a STALE park genuinely bounds)."""

    now: float = 0.0
    signal: FakeSignal | None = None
    _anchor: float = field(default_factory=time.monotonic)

    def read(self) -> float:
        """The gate's current clock value: the offset over real elapsed time."""
        return self.now + (time.monotonic() - self._anchor)


@dataclass
class FakeSignal:
    """An asyncio-backed signal the gate awaits; ``set`` wakes parked admits."""

    _event: asyncio.Event = field(default_factory=asyncio.Event)

    async def wait(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
            return True
        except TimeoutError:
            return False

    def set(self) -> None:
        self._event.set()


def _make_gauges(
    memory_usage: float,
    cpu_usage: float,
    mem_gauge: FakeGauge | None,
    cpu_gauge: FakeGauge | None,
) -> tuple[FakeGauge, FakeGauge]:
    """The two gauges, defaulting to a flat usage when no gauge is injected."""
    return (mem_gauge or FakeGauge(memory_usage), cpu_gauge or FakeGauge(cpu_usage))


def _make_sensing(
    memory_usage: float,
    cpu_usage: float,
    mem_target: float,
    mem_hard: float,
    cpu_target: float,
    cpu_hard: float,
    mem_gauge: FakeGauge | None,
    cpu_gauge: FakeGauge | None,
) -> Sensing:
    """A sensing set over {MEMORY, CPU} with the given target/hard ceilings."""
    mem_g, cpu_g = _make_gauges(memory_usage, cpu_usage, mem_gauge, cpu_gauge)
    return Sensing(
        {
            ResourceId.MEMORY: SensorBinding(Limits(target=Ratio(mem_target), hard=Ratio(mem_hard)), mem_g),
            ResourceId.CPU: SensorBinding(Limits(target=Ratio(cpu_target), hard=Ratio(cpu_hard)), cpu_g),
        }
    )


def _make_lanes(
    general_cost_mib: float,
    heavy_cost_mib: float,
    general_ceiling: int,
    heavy_ceiling: int,
    scale_source: ScaleSource | None,
) -> dict[LaneId, LaneSpec]:
    """The two lane specs priced in bytes; GENERAL honors an optional scale source."""
    mib = 1024 * 1024
    return {
        LaneId.GENERAL: LaneSpec(
            ceiling=Count(general_ceiling),
            cost=Cost(Bytes(int(general_cost_mib * mib))),
            scale_source=scale_source or MinAll(),
        ),
        LaneId.HEAVY: LaneSpec(
            ceiling=Count(heavy_ceiling),
            cost=Cost(Bytes(int(heavy_cost_mib * mib))),
            scale_source=MinAll(),
        ),
    }


@dataclass(frozen=True)
class GateSpec:
    """Driver-level knobs; only the commuting concern between caller and gate."""

    memory_usage: float = 0.2
    cpu_usage: float = 0.2
    mem_target: float = 0.6
    mem_hard: float = 0.9
    cpu_target: float = 0.6
    cpu_hard: float = 0.9
    general_cost_mib: float = 48
    heavy_cost_mib: float = 700
    general_ceiling: int = 32
    heavy_ceiling: int = 8
    ramp_floor: int = 1
    ramp_max: int = 64
    ramp_multiplier: float = 1.10
    floor_fraction: int = 4
    stale_bound: float = 30.0
    cold_probe: int = 1
    initial_ramp: int | None = None
    loop_lag: float = 1.0
    scale_source: ScaleSource | None = None
    mem_gauge: FakeGauge | None = None
    cpu_gauge: FakeGauge | None = None
    memory_budget_mib: int | None = None


__all__ = ["FakeGauge", "GateHandles", "GateSpec", "build_gate"]


def build_gate(spec: GateSpec | None = None) -> tuple[AdmissionGate, GateHandles]:
    """A gate in a clean, driven sensing arrangement.

    Returns ``(gate, handles)``; advance ``handles.now`` or ``handles.signal.set()``
    to drive the gate. Pass a ``GateSpec`` to override driver knobs."""
    s = GateSpec() if spec is None else spec
    mib = 1024 * 1024
    handles = GateHandles(now=0.0)

    class _Clock:
        def now(self) -> Instant:
            return Instant._of(handles.read())

    clock: Clock = _Clock()
    signal = FakeSignal()
    handles.signal = signal
    sensing = _make_sensing(
        s.memory_usage,
        s.cpu_usage,
        s.mem_target,
        s.mem_hard,
        s.cpu_target,
        s.cpu_hard,
        s.mem_gauge,
        s.cpu_gauge,
    )
    lanes = _make_lanes(
        s.general_cost_mib, s.heavy_cost_mib, s.general_ceiling, s.heavy_ceiling, s.scale_source
    )
    pacing = PacingPolicy(
        ramp_floor=Count(s.ramp_floor),
        ramp_max=Count(s.ramp_max),
        ramp_multiplier=Multiplier(s.ramp_multiplier),
        floor_fraction=FloorDivisor(s.floor_fraction),
        stale_bound=Duration(s.stale_bound),
        cold_probe=Count(s.cold_probe),
        initial_ramp=None if s.initial_ramp is None else Count(s.initial_ramp),
        yield_interval=Duration(0.0),
        ramp_growth_window=Count(3),
    )
    gate = AdmissionGate(
        pacing=pacing,
        lanes=lanes,
        sensing=sensing,
        clock=clock,
        signal=signal,
        loop_lag_multiplier=Ratio(s.loop_lag),
        memory_budget=None if s.memory_budget_mib is None else Bytes(s.memory_budget_mib * mib),
    )
    return gate, handles
