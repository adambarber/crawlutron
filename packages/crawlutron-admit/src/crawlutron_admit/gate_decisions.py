"""The gate's decision logic, split from its door and state commits.

Three focused mixins: sensing/scale, capacity/reconciliation, and ramp/pacing.
The gate composes them; no decision method holds the state lock.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

from crawlutron_admit.policy import LaneSpec, PacingPolicy
from crawlutron_admit.seams import Clock
from crawlutron_admit.values import (
    Allocation,
    Bytes,
    Instant,
    LaneId,
    Limits,
    Progress,
    Ratio,
    ResourceId,
    RideCpuWhenFree,
    Scale,
    ScaleSource,
    Sensing,
    Want,
)

_logger = logging.getLogger(__name__)

__all__ = ["Decision", "DecisionKind"]


class DecisionKind(Enum):
    """The private decision vocabulary; callers only ever see IdleReason terminals."""

    GRANT = "grant"
    PARK = "park"
    STALE = "stale"
    OVERSIZE = "oversize"


@dataclass(frozen=True)
class Decision:
    """One admission decision: the kind plus whatever the commit needs."""

    kind: DecisionKind
    per_lane: dict[LaneId, int] | None = None
    scale: Scale | None = None
    remaining_stale: float = 0.0
    cold: bool = False


@dataclass(frozen=True)
class _ResourceReading:
    """One resource's snapshotted sensing state for a single decision (§7.2)."""

    usage: Ratio
    warm: bool
    stale: bool
    cold: bool


class SensingMixin:
    """Snapshots gauges and derives the effective scale once per decision."""

    _pacing: PacingPolicy
    _sensing: Sensing
    _loop_lag: Callable[[], Ratio]
    _lanes: dict[LaneId, LaneSpec]

    def _snapshot_readings(self, now: float) -> dict[ResourceId, _ResourceReading]:
        read: dict[ResourceId, _ResourceReading] = {}
        for resource, binding in self._sensing.sensors.items():
            gauge = binding.gauge
            usage = Ratio(1.0)
            warm = False
            age = 0.0
            try:
                warm = gauge.is_warm()
                age = gauge.last_sample_age(now)
                usage = gauge.read_usage()
            except Exception:
                self._gauge_failure(resource)
                warm = False
                age = float("inf")
            stale = not warm and age > self._pacing.stale_bound.value
            read[resource] = _ResourceReading(
                usage=usage, warm=warm, stale=stale, cold=not warm and not stale
            )
        return read

    def _gauge_failure(self, _resource: ResourceId) -> None:
        """A failing sensor is logged but never breaks admission."""
        with contextlib.suppress(Exception):
            _logger.warning(
                "admission gauge %s failed; hard-saturated and marked stale",
                _resource,
                exc_info=True,
            )

    def _effective_scale(self, source: ScaleSource, readings: dict[ResourceId, _ResourceReading]) -> Scale:
        """The single effective-scale law: min-all or RIDE, times loop-lag."""
        mem = self._scale_of(readings[ResourceId.MEMORY], self._sensing.sensors[ResourceId.MEMORY].limits)
        cpu = self._scale_of(readings[ResourceId.CPU], self._sensing.sensors[ResourceId.CPU].limits)
        if isinstance(source, RideCpuWhenFree):
            cpu_usage = readings[ResourceId.CPU].usage.value
            base = cpu if 1.0 - cpu_usage > source.threshold.value else Ratio(min(mem.value, cpu.value))
        else:
            base = Ratio(min(mem.value, cpu.value))
        lag = self._loop_lag()
        return Scale(max(0.0, base.value) * lag.value)

    def _scale_for(self, lane: LaneId, readings: dict[ResourceId, _ResourceReading]) -> Scale:
        """The effective per-lane scale under the lane's source."""
        return self._effective_scale(self._lanes[lane].scale_source, readings)

    def _scale_of(self, reading: _ResourceReading, limits: Limits) -> Ratio:
        """Full at/by target, zero at/by hard, linear between."""
        usage = reading.usage.value
        target = limits.target.value
        hard = limits.hard.value
        if usage <= target:
            return Ratio(1.0)
        if usage >= hard:
            return Ratio(0.0)
        return Ratio(1.0 - (usage - target) / (hard - target))

    def _lane_stale(self, readings: dict[ResourceId, _ResourceReading]) -> bool:
        """A lane is stale when any depended resource is stale (fail-closed)."""
        return readings[ResourceId.MEMORY].stale or readings[ResourceId.CPU].stale

    def _lane_cold(self, readings: dict[ResourceId, _ResourceReading]) -> bool:
        """Cold when any non-stale resource lacks a current sample."""
        return any(r.cold for r in readings.values())


class CapacityMixin(SensingMixin):
    """Reserves capacity over one memory pool and decides the grant."""

    _pacing: PacingPolicy
    _lanes: dict[LaneId, LaneSpec]
    _memory_budget: Bytes
    _reserved: Bytes
    _ramp: int
    _in_flight: dict
    _in_flight_bytes: int
    _cold_in_flight: int
    _staleness_onset: float | None

    def _free_bytes(self) -> int:
        """Bytes left under the hard budget after reservations."""
        return max(0, self._memory_budget.value - self._reserved.value - self._in_flight_bytes)

    def _capacity_for(self, lane: LaneId, want_count: int, scale: Scale) -> int:
        """ADDITIONAL capacity: TOTAL ceiling/scale minus in-flight."""
        spec = self._lanes[lane]
        scale_slots = int(round(spec.ceiling.value * scale.value)) - self._in_flight[lane]
        ceiling = spec.ceiling.value - self._in_flight[lane]
        bounds = [scale_slots, ceiling, want_count]
        unit = spec.cost.memory_bytes.value
        if unit > 0:
            bounds.append(self._free_bytes() // unit)
        return max(0, min(bounds))

    def _reconcile(
        self, wanted: list[LaneId], capacity: dict[LaneId, int], floor_for: dict[LaneId, int]
    ) -> dict[LaneId, int]:
        """Grant largest units first from ONE remaining pool."""
        granted: dict[LaneId, int] = dict.fromkeys(LaneId, 0)
        free = self._free_bytes()
        order = sorted(LaneId, key=lambda lane: self._lanes[lane].cost.memory_bytes.value, reverse=True)
        for lane in order:
            unit = self._lanes[lane].cost.memory_bytes.value
            if lane not in wanted or capacity[lane] < 1:
                continue
            count = min(capacity[lane], floor_for[lane])
            if unit > 0:
                count = min(count, free // unit)
            granted[lane] = max(0, count)
            free -= granted[lane] * unit
        return granted

    def _decide(self, want: Want, readings: dict[ResourceId, _ResourceReading], now: float) -> Decision:
        """Decide under the lock: STALE → OVERSIZE → RETRY_PRESSURE → GRANT."""
        wanted = [lane for lane in LaneId if want.lanes[lane].value >= 1]
        if self._lane_stale(readings) and wanted:
            onset = now if self._staleness_onset is None else self._staleness_onset
            self._staleness_onset = onset
            remaining = max(0.0, self._pacing.stale_bound.value - (now - onset))
            return Decision(kind=DecisionKind.STALE, remaining_stale=remaining)
        self._staleness_onset = None

        in_flight = any(self._in_flight[lane] > 0 for lane in LaneId)
        cold = self._lane_cold(readings)
        capacity: dict[LaneId, int] = {}
        scale_map: dict[LaneId, Scale] = {}
        for lane in LaneId:
            scale = self._scale_for(lane, readings)
            scale_map[lane] = scale
            capacity[lane] = self._capacity_for(lane, want.lanes[lane].value, scale)
        oversize = bool(wanted) and all(capacity[lane] < 1 for lane in wanted) and not in_flight
        if oversize:
            return Decision(kind=DecisionKind.OVERSIZE, scale=scale_map[LaneId.GENERAL], cold=cold)
        cold_probe_exhausted = cold and self._cold_in_flight >= self._pacing.cold_probe.value
        wanted_fits = any(capacity[lane] >= 1 for lane in wanted)
        if cold_probe_exhausted and wanted_fits:
            return Decision(kind=DecisionKind.PARK, scale=scale_map[LaneId.GENERAL], cold=cold)

        floor_global = max(1, self._ramp // self._pacing.floor_fraction.value)
        lane_spread = max(1, len(wanted))
        floor_for = {
            lane: max(1, min(want.lanes[lane].value, floor_global // lane_spread)) for lane in LaneId
        }
        granted: dict[LaneId, int] = self._reconcile(wanted, capacity, floor_for)
        if sum(granted.values()) < 1:
            return Decision(kind=DecisionKind.PARK, scale=scale_map[LaneId.GENERAL], cold=cold)
        return Decision(
            kind=DecisionKind.GRANT,
            per_lane=granted,
            scale=scale_map[LaneId.GENERAL],
            cold=cold,
        )


class RampMixin(SensingMixin):
    """Owns ramp growth/decay and the grant progress record."""

    _pacing: PacingPolicy
    _ramp: int
    _ramp_floor: int
    _ramp_max: int
    _scale_full_streak: int
    _progress: Progress
    _clock: Clock
    _lanes: dict[LaneId, LaneSpec]

    def _record_decision(
        self, now: float, allocation: Allocation, *, cold: bool = False, scale: Scale
    ) -> None:
        """Ramp growth/decay and progress advance for one granting decision."""
        self._progress = Progress(
            last_advance=Instant.of(max(now, self._progress.last_advance.value)),
            last_decision=Instant.of(max(now, self._progress.last_decision.value)),
        )
        if cold or scale.value < 1.0:
            self._scale_full_streak = 0
            return
        self._scale_full_streak += 1
        if self._scale_full_streak >= self._pacing.ramp_growth_window.value:
            self._scale_full_streak = 0
            grown = int(self._ramp * self._pacing.ramp_multiplier.value)
            self._ramp = min(self._ramp_max, grown)

    def _decay_ramp(self) -> None:
        """Decay below full scale: ramp = max(ramp_floor, ramp*scale)."""
        read = self._snapshot_readings(self._clock.now().value)
        scale = self._effective_scale(self._lanes[LaneId.GENERAL].scale_source, read).value
        self._ramp = max(self._ramp_floor, int(self._ramp * scale))
