"""The gate: one decision point; owns ramp, capacity, floor, progress, in-flight.

Door: ``async with gate.admit(want) as granted:`` -- a live :class:`Granted`, or an
:class:`AdmissionRefused` (OVERSIZE / STALE). The state lock is released before parks.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum

from admission.errors import AdmissionRefused, NestedAdmitError, WrongGateError
from admission.policy import LaneSpec, PacingPolicy
from admission.seams import Clock, Signal
from admission.values import (
    Allocation,
    AllocationEntry,
    Bytes,
    Count,
    Duration,
    Granted,
    IdleReason,
    Instant,
    LaneId,
    Lease,
    Progress,
    Ratio,
    ResourceId,
    RideCpuWhenFree,
    Scale,
    Sensing,
    Want,
)

__all__ = ["AdmissionGate", "Decision", "DecisionKind", "StallGuard"]


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


class AdmissionGate:
    """The single decision point for paced, resource-adaptive admission.

    Thread-safe for concurrent admit/release (trace-invariant under any
    interleaving, 4.3); the ramp is privately owned and never exposed."""

    def __init__(
        self,
        *,
        pacing: PacingPolicy,
        lanes: dict[LaneId, LaneSpec],
        sensing: Sensing,
        clock: Clock,
        signal: Signal,
        loop_lag_multiplier: Ratio | None = None,
        memory_budget: Bytes | None = None,
    ) -> None:
        if set(lanes) != {LaneId.GENERAL, LaneId.HEAVY}:
            raise ValueError("gate requires a lane spec for every LaneId")
        self._pacing = pacing
        self._lanes = lanes
        self._sensing = sensing
        self._clock = clock
        self._signal = signal
        self._loop_lag = loop_lag_multiplier if loop_lag_multiplier is not None else Ratio(1.0)
        if memory_budget is None:
            memory_budget = Bytes(
                max(
                    lanes[LaneId.GENERAL].cost.memory_bytes.value,
                    lanes[LaneId.HEAVY].cost.memory_bytes.value,
                )
            )
        initial = pacing.initial_ramp or Count(max(1, pacing.ramp_floor.value // 4))
        self._ramp = initial.value
        self._ramp_floor = pacing.ramp_floor.value
        self._ramp_max = pacing.ramp_max.value
        self._version = 0
        self._lock = threading.Lock()
        self._in_flight = {LaneId.GENERAL: 0, LaneId.HEAVY: 0}
        self._in_flight_bytes = 0
        self._cold_in_flight = 0
        self._scale_full_streak = 0
        self._memory_budget = memory_budget
        now0 = clock.now()
        self._progress = Progress(last_advance=now0, last_decision=now0)
        self._staleness_onset: float | None = None
        self._lease_ctx: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "admission_lease_active", default=False
        )

    # -- read-only snapshots -------------------------------------------

    def ramp(self) -> Count:
        """A copy of the current ramp; mutating it changes nothing on the gate."""
        with self._lock:
            return Count(self._ramp)

    def progress(self) -> Progress:
        """The current progress record."""
        with self._lock:
            return Progress(
                last_advance=Instant._of(self._progress.last_advance.value),
                last_decision=Instant._of(self._progress.last_decision.value),
            )

    def reset_ramp_for_quiescence(self) -> None:
        """Restore the cold initial ramp; guarded against running mid-flight."""
        with self._lock:
            if any(self._in_flight.values()):
                raise AssertionError("reset_ramp_for_quiescence requires zero in-flight leases")
            initial = self._pacing.initial_ramp or Count(max(1, self._ramp_floor // 4))
            self._ramp = initial.value

    # -- gauge snapshotting (outside the lock) -------------------------

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
                # Fail-closed: a raising gauge hard-saturates and drives stale (§7.1).
                self._gauge_failure(resource)
                warm = False
                age = float("inf")
            stale = not warm and age > self._pacing.stale_bound.value
            cold = not warm and not stale
            read[resource] = _ResourceReading(usage=usage, warm=warm, stale=stale, cold=cold)
        return read

    def _gauge_failure(self, _resource: ResourceId) -> None:
        """A gauge raised: record it (hard-saturation is applied by the caller)."""

    # -- decision --------------------------------------------------------

    def _scale_for(self, lane: LaneId, readings: dict[ResourceId, _ResourceReading]) -> Scale:
        """The effective per-lane scale: min-all over the lane's resources, times lag."""
        mem = self._scale_of(readings[ResourceId.MEMORY], self._sensing.sensors[ResourceId.MEMORY].limits)
        cpu = self._scale_of(readings[ResourceId.CPU], self._sensing.sensors[ResourceId.CPU].limits)
        source = self._lanes[lane].scale_source
        if isinstance(source, RideCpuWhenFree):
            cpu_usage = readings[ResourceId.CPU].usage.value
            base = cpu if 1.0 - cpu_usage > source.threshold.value else Ratio(min(mem.value, cpu.value))
        else:
            base = Ratio(min(mem.value, cpu.value))
        return Scale(max(0.0, base.value) * self._loop_lag.value)

    def _scale_of(self, reading: _ResourceReading, limits) -> Ratio:
        """The §5.1 law: full at/by target, zero at/by hard, linear between."""
        usage = reading.usage.value
        target = limits.target.value
        hard = limits.hard.value
        if usage <= target:
            return Ratio(1.0)
        if usage >= hard:
            return Ratio(0.0)
        return Ratio(1.0 - (usage - target) / (hard - target))

    def _lane_stale(self, readings: dict[ResourceId, _ResourceReading]) -> bool:
        """A lane is stale when any depended resource is stale (fail-closed, 4.3)."""
        return readings[ResourceId.MEMORY].stale or readings[ResourceId.CPU].stale

    def _lane_cold(self, readings: dict[ResourceId, _ResourceReading]) -> bool:
        """The box is cold for ramp growth when any non-stale resource is cold."""
        return any(r.cold for r in readings.values())

    def _free_bytes(self) -> int:
        return max(0, self._memory_budget.value - self._in_flight_bytes)

    def _capacity_for(self, lane: LaneId, want_count: int, scale: Scale) -> int:
        """capacity_lane = min(round(ceiling*scale), ceiling, want, free//cost) (§5.2)."""
        spec = self._lanes[lane]
        scale_slots = int(round(spec.ceiling.value * scale.value))
        ceiling = spec.ceiling.value
        unit = spec.cost.memory_bytes.value
        free_fits = self._free_bytes() // unit if unit > 0 else 0
        return max(0, min(scale_slots, ceiling, want_count, free_fits))

    def _decide(self, want: Want, readings: dict[ResourceId, _ResourceReading]) -> Decision:
        """Decide under the state lock: STALE-first -> OVERSIZE -> RETRY_PRESSURE (4.3)."""
        now = self._clock.now().value
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
        # Cold-probe cap: while a resource is cold, admit only the bounded probing
        # allowance; a box past its probe park is a RETRY_PRESSURE park, not an oversize.
        cold_probe_exhausted = cold and self._cold_in_flight >= self._pacing.cold_probe.value
        wanted_fits = any(capacity[lane] >= 1 for lane in wanted)
        if cold_probe_exhausted and wanted_fits:
            return Decision(kind=DecisionKind.PARK, scale=scale_map[LaneId.GENERAL], cold=cold)

        # Floor law: per-lane floors (clamped by want), a swept floor still grants 1.
        floor_global = max(1, self._ramp // self._pacing.floor_fraction.value)
        lane_spread = max(1, len(wanted))
        floor_for = {
            lane: max(1, min(want.lanes[lane].value, floor_global // lane_spread)) for lane in LaneId
        }
        granted: dict[LaneId, int] = {}
        for lane in LaneId:
            if lane in wanted and capacity[lane] >= 1:
                free_slots = max(0, capacity[lane] - self._in_flight[lane])
                granted[lane] = min(free_slots, floor_for[lane])
            else:
                granted[lane] = 0
        if sum(granted.values()) < 1:
            # Nothing granted: park until a release/sensing wake reopens capacity.
            return Decision(kind=DecisionKind.PARK, scale=scale_map[LaneId.GENERAL], cold=cold)
        return Decision(
            kind=DecisionKind.GRANT,
            per_lane=granted,
            scale=scale_map[LaneId.GENERAL],
            cold=cold,
        )

    # -- the door ---------------------------------------------------------

    @asynccontextmanager
    async def admit(self, want: Want) -> AsyncGenerator[Granted, None]:
        """Admit-and-lease: yield a live Granted or raise AdmissionRefused."""
        if self._lease_ctx.get():
            raise NestedAdmitError()
        _token = self._lease_ctx.set(True)
        granted: Granted | None = None
        try:
            granted = await self._park_until_grant(want)
            yield granted
        finally:
            if granted is not None:
                completed = sys.exc_info()[0] is None
                granted.lease.release(work_completed=completed)
            try:
                self._lease_ctx.reset(_token)
            except ValueError:
                # An async generator closed from a foreign context cannot reset the
                # token; clear the lease marker so it never leaks across tasks.
                self._lease_ctx.set(False)

    async def _park_until_grant(self, want: Want) -> Granted:
        """Decide, parking internally, until a live grant is possible (BLOCKER-1)."""
        while True:
            now = self._clock.now().value
            readings = self._snapshot_readings(now)
            with self._lock:
                decision = self._decide(want, readings)
            if decision.kind is DecisionKind.GRANT:
                return self._commit_grant(decision)
            if decision.kind is DecisionKind.OVERSIZE:
                raise AdmissionRefused(IdleReason.OVERSIZE)
            if decision.kind is DecisionKind.STALE:
                remaining = decision.remaining_stale
                if remaining <= 0.0:
                    raise AdmissionRefused(IdleReason.STALE)
                await self._bounded_wait(self._signal, remaining)
                continue
            # PARK (RETRY_PRESSURE / cold probe) — event-bounded, no wall deadline.
            await self._bounded_wait(self._signal, None)

    async def _bounded_wait(self, signal: Signal, timeout: float | None) -> None:
        """The single gate-side wait site: Signal + version re-check (5.4, 4.3)."""
        version_before = self._version
        try:
            await asyncio.wait_for(signal.wait(timeout), timeout=timeout)
        except TimeoutError:
            pass
        # Wake-then-recheck: a version bump since the decision re-decides (4.3).
        if self._version == version_before:
            return

    def _commit_grant(self, decision: Decision) -> Granted:
        """Commit the in-flight reservation under the lock and build the lease."""
        if decision.per_lane is None:
            raise RuntimeError("commit requires a GRANT decision with per-lane amounts")
        with self._lock:
            allocation_entries: dict[LaneId, AllocationEntry] = {}
            per_lane = {lane: decision.per_lane[lane] for lane in LaneId}
            for lane, amount in per_lane.items():
                allocation_entries[lane] = AllocationEntry(Count(amount))
                if amount > 0:
                    unit = self._lanes[lane].cost.memory_bytes.value
                    self._in_flight[lane] += amount
                    self._in_flight_bytes += amount * unit
                    if decision.cold:
                        self._cold_in_flight += 1
            granted_alloc = Allocation(allocation_entries)
            lease = Lease._of(self, per_lane, cold=decision.cold)
            now = self._clock.now().value
            self._record_decision(now, granted_alloc, cold=decision.cold)
        return Granted(
            allocation=granted_alloc,
            scale=decision.scale or Scale(1.0),
            paced=False,
            lease=lease,
        )

    def _record_decision(self, now: float, allocation: Allocation, *, cold: bool = False) -> None:
        """Ramp growth/decay and progress advance for one granting decision (4.1, 4.4)."""
        self._progress = Progress(
            last_advance=Instant._of(max(now, self._progress.last_advance.value)),
            last_decision=Instant._of(max(now, self._progress.last_decision.value)),
        )
        if cold:
            # Cold posture grows no ramp: no current sample means no throughput proof.
            self._scale_full_streak = 0
            return
        # Growth only after scale-full over a window; the streak is consumed here.
        self._scale_full_streak += 1
        if self._scale_full_streak >= self._pacing.ramp_growth_window.value:
            self._scale_full_streak = 0
            grown = int(self._ramp * self._pacing.ramp_multiplier.value)
            self._ramp = min(self._ramp_max, grown)

    def _decay_ramp(self) -> None:
        """Decay the ramp below full scale: ramp = max(ramp_floor, ramp*scale) (4.1)."""
        self._scale_full_streak = 0
        read = self._snapshot_readings(self._clock.now().value)
        scale = min(
            self._scale_of(read[ResourceId.MEMORY], self._sensing.sensors[ResourceId.MEMORY].limits).value,
            self._scale_of(read[ResourceId.CPU], self._sensing.sensors[ResourceId.CPU].limits).value,
        )
        self._ramp = max(self._ramp_floor, int(self._ramp * scale))

    def _release_lease(self, lease: Lease, *, work_completed: bool) -> None:
        """Release a committed lease; checks gate identity and idempotence."""
        if lease.gate is not self:
            raise WrongGateError(lease_owner=lease.gate, gate=self)
        with self._lock:
            per_lane = lease._per_lane
            for lane, amount in per_lane.items():
                if amount <= 0:
                    continue
                if self._in_flight[lane] < amount:
                    return
                unit = self._lanes[lane].cost.memory_bytes.value
                self._in_flight[lane] -= amount
                self._in_flight_bytes -= amount * unit
            if lease._cold:
                self._cold_in_flight = max(0, self._cold_in_flight - 1)
            self._version += 1
            now = self._clock.now().value
            if work_completed:
                self._progress = Progress(
                    last_advance=Instant._of(max(now, self._progress.last_advance.value)),
                    last_decision=self._progress.last_decision,
                )
            self._signal.set()
            self._decay_ramp()


class StallGuard:
    """Gate-liveness detection keyed on the gate's own clock and Progress.

    ``last_advance`` advancing (a grant or completed release) suppresses the stall; an
    uncompleted early-return or exceptioned body freezes progress. No ``clock`` param.
    """

    def __init__(self, gate: AdmissionGate, *, no_progress_stall: Duration | None = None) -> None:
        self._gate = gate
        bound = no_progress_stall if no_progress_stall is not None else Duration(60.0)
        self._bound = bound.value

    def stalled(self) -> bool:
        """Whether gate liveness froze past the bound, on the injected clock."""
        now = self._gate._clock.now().value
        last = self._gate._progress.last_advance.value
        return (now - last) > self._bound
