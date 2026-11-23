"""The gate: one decision point; owns ramp, capacity, floor, progress, in-flight.

Door: ``async with gate.admit(want) as granted:`` -- a live :class:`Granted`, or an
:class:`AdmissionRefused` (OVERSIZE / STALE). The state lock is released before parks.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import threading
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager

from crawlutron_admit.errors import AdmissionRefused, NestedAdmitError, WrongGateError
from crawlutron_admit.gate_decisions import CapacityMixin, Decision, DecisionKind, RampMixin
from crawlutron_admit.policy import LaneSpec, PacingPolicy
from crawlutron_admit.seams import Clock, Signal
from crawlutron_admit.values import (
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
    Scale,
    Sensing,
    Want,
)

__all__ = ["AdmissionGate", "Decision", "DecisionKind", "StallGuard"]


class AdmissionGate(CapacityMixin, RampMixin):
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
        loop_lag: Callable[[], Ratio] | None = None,
        memory_budget: Bytes | None = None,
        reserved: Bytes | None = None,
    ) -> None:
        if set(lanes) != {LaneId.GENERAL, LaneId.HEAVY}:
            raise ValueError("gate requires a lane spec for every LaneId")
        self._pacing = pacing
        self._lanes = lanes
        self._sensing = sensing
        self._clock = clock
        self._signal = signal
        self._loop_lag = loop_lag if loop_lag is not None else (lambda: Ratio(1.0))
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
        self._reserved = reserved if reserved is not None else Bytes(0)
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

    def cold_in_flight(self) -> Count:
        """The number of cold-probe grants currently live (one per granting decision)."""
        with self._lock:
            return Count(self._cold_in_flight)

    def progress(self) -> Progress:
        """The current progress record."""
        with self._lock:
            return Progress(
                last_advance=Instant.of(self._progress.last_advance.value),
                last_decision=Instant.of(self._progress.last_decision.value),
            )

    def now(self) -> Instant:
        """The gate's current clock reading, for liveness checks."""
        return self._clock.now()

    def reset_ramp_for_quiescence(self) -> None:
        """Restore the cold initial ramp; guarded against running mid-flight."""
        with self._lock:
            if any(self._in_flight.values()):
                raise RuntimeError("reset_ramp_for_quiescence requires zero in-flight leases")
            initial = self._pacing.initial_ramp or Count(max(1, self._ramp_floor // 4))
            self._ramp = initial.value

    # -- the door ---------------------------------------------------------

    @asynccontextmanager
    async def admit(self, want: Want) -> AsyncGenerator[Granted]:
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
        """Decide-and-commit as one atomic lock hold; a live grant returns within it."""
        while True:
            now = self._clock.now().value
            readings = self._snapshot_readings(now)
            with self._lock:
                decision = self._decide(want, readings, now)
                if decision.kind is DecisionKind.GRANT:
                    # Decide and commit under one lock hold (B1): a released lock
                    # between them let two admits over-commit the ceiling.
                    return self._commit_grant_locked(decision)
            if decision.kind is DecisionKind.OVERSIZE:
                raise AdmissionRefused(IdleReason.OVERSIZE)
            if decision.kind is DecisionKind.STALE:
                remaining = decision.remaining_stale
                if remaining <= 0.0:
                    raise AdmissionRefused(IdleReason.STALE)
                await self._bounded_wait(self._signal, remaining)
                continue
            # PARK (RETRY_PRESSURE / cold probe) — re-checks under the stale
            # bound so a signal-less park cannot hang forever.
            await self._bounded_wait(self._signal, self._pacing.stale_bound.value)

    async def _bounded_wait(self, signal: Signal, timeout: float | None) -> None:
        """The single gate-side wait site: Signal, optional wall deadline (5.4)."""
        try:
            await asyncio.wait_for(signal.wait(timeout), timeout=timeout)
        except TimeoutError:
            pass

    def _commit_grant_locked(self, decision: Decision) -> Granted:
        """Commit an already-decided GRANT; the caller holds the state lock."""
        if decision.per_lane is None:
            raise RuntimeError("commit requires a GRANT decision with per-lane amounts")
        allocation_entries: dict[LaneId, AllocationEntry] = {}
        per_lane = {lane: decision.per_lane[lane] for lane in LaneId}
        for lane, amount in per_lane.items():
            allocation_entries[lane] = AllocationEntry(Count(amount))
            if amount > 0:
                unit = self._lanes[lane].cost.memory_bytes.value
                self._in_flight[lane] += amount
                self._in_flight_bytes += amount * unit
        # One cold-probe debit per DECISION (any cold lane the grant spans), NOT
        # per lane: a dual-lane cold grant consumes one slot, released once (§7.2).
        if decision.cold:
            self._cold_in_flight += 1
        granted_alloc = Allocation(allocation_entries)
        now = self._clock.now().value
        decision_scale = decision.scale or Scale(1.0)
        lease = Lease._of(self, per_lane, cold=decision.cold, scale=decision_scale)
        self._record_decision(now, granted_alloc, cold=decision.cold, scale=decision_scale)
        return Granted(
            allocation=granted_alloc,
            scale=decision_scale,
            lease=lease,
        )

    def _release_lease(self, lease: Lease, *, work_completed: bool) -> None:
        """Release a committed lease; checks gate identity and idempotence."""
        if lease.gate is not self:
            raise WrongGateError(lease_owner=lease.gate, gate=self)
        with self._lock:
            per_lane = lease._per_lane
            # Validate every lane before decrementing ANY (B2): a silent mid-loop
            # return half-releases and starves parked waiters. Mismatch is corruption.
            for lane, amount in per_lane.items():
                if amount > 0 and self._in_flight[lane] < amount:
                    raise RuntimeError(
                        f"release counter mismatch on {lane}: "
                        f"in_flight {self._in_flight[lane]} < lease {amount}"
                    )
            for lane, amount in per_lane.items():
                if amount <= 0:
                    continue
                unit = self._lanes[lane].cost.memory_bytes.value
                self._in_flight[lane] -= amount
                self._in_flight_bytes -= amount * unit
            if lease._cold:
                self._cold_in_flight = max(0, self._cold_in_flight - 1)
            self._version += 1
            now = self._clock.now().value
            if work_completed:
                self._progress = Progress(
                    last_advance=Instant.of(max(now, self._progress.last_advance.value)),
                    last_decision=self._progress.last_decision,
                )
            self._signal.set()
            # A grant that ran at full scale did not decay the ramp during its
            # lease, so the release skips the (re-reading) decay pass (M2).
            if lease._grant_scale.value < 1.0:
                self._decay_ramp()


class StallGuard:
    """Gate-liveness detection keyed on the gate's own clock and progress.

    ``last_advance`` advancing (a grant or a completed release) keeps the gate live;
    a stall freezes progress past the bound. No ``clock`` param.
    """

    def __init__(self, gate: AdmissionGate, *, no_progress_stall: Duration | None = None) -> None:
        self._gate = gate
        bound = no_progress_stall if no_progress_stall is not None else Duration(60.0)
        self._bound = bound.value

    def stalled(self) -> bool:
        """Whether gate liveness froze past the bound, on the injected clock."""
        now = self._gate.now()
        last = self._gate.progress().last_advance
        return (now.value - last.value) > self._bound
