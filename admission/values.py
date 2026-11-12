"""The kernel's closed value surface: every quantity is a typed, validated object.

Construction is admission -- an illegal state fails at build, never silently
(NormalizedUrl bar). The kernel is call-context-free: no I/O, no caller assumptions.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from admission.gate import AdmissionGate

__all__ = [
    "Allocation",
    "AllocationEntry",
    "Bytes",
    "Cost",
    "Count",
    "Duration",
    "FloorDivisor",
    "Granted",
    "Instant",
    "LaneId",
    "Lease",
    "Limits",
    "MinAll",
    "Multiplier",
    "Progress",
    "Ratio",
    "ResourceId",
    "RideCpuWhenFree",
    "Scale",
    "ScaleSource",
    "SensorBinding",
    "Sensing",
    "Want",
]


class _Value:
    """Value base: equality/hash/repr over the single ``value`` slot."""

    __slots__ = ("value",)
    value: Any

    def __eq__(self, other: object) -> bool:
        if type(self) is not type(other):
            return NotImplemented
        this = cast(Any, self).value
        that = cast(Any, other).value
        return this == that

    def __hash__(self) -> int:
        return hash(self.value)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.value!r})"


def _finite_float(value: float, name: str, *, lo: float, hi: float | None) -> float:
    """Coerce + validate a finite float within ``[lo, hi]`` (unbounded when ``hi`` is None)."""
    v = float(value) if not isinstance(value, float) else value
    if hi is None:
        bounded = math.isfinite(v) and v >= lo
        detail = f">= {lo}"
    else:
        bounded = math.isfinite(v) and lo <= v <= hi
        detail = f"in [{lo}, {hi}]"
    if not bounded:
        raise ValueError(f"{name} must be {detail} and finite, got {v!r}")
    return v


def _int_ge(value: int, name: str, *, minimum: int, reject_bool: bool = True) -> int:
    """Validate that ``value`` is an int at least ``minimum``, refusing booleans optionally."""
    if not isinstance(value, int) or (reject_bool and isinstance(value, bool)):
        raise ValueError(f"{name} must be an int, got {value!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value!r}")
    return value


class ResourceId(enum.StrEnum):
    """The closed resource vocabulary: MEMORY/CPU/LOOP_LAG."""

    MEMORY = "memory"
    CPU = "cpu"
    LOOP_LAG = "loop_lag"

    @classmethod
    def members(cls) -> tuple[ResourceId, ...]:
        """The complete closed set, for construction checks."""
        return tuple(cls)

    @classmethod
    def from_value(cls, value: str) -> ResourceId:
        """Case-exact mapping from wire value to member (LOOP_LAG requires the underscore)."""
        return cls(value)


class LaneId(enum.StrEnum):
    """The closed lane vocabulary: GENERAL (HTTP) and HEAVY (browser).

    Crawlutron maps HTTP->GENERAL and BROWSER->HEAVY at the seam only, never
    here.
    """

    GENERAL = "general"
    HEAVY = "heavy"

    @classmethod
    def members(cls) -> tuple[LaneId, ...]:
        """The complete closed set, for construction checks."""
        return tuple(cls)

    @classmethod
    def from_value(cls, value: str) -> LaneId:
        """Case-exact mapping from wire value to member."""
        return cls(value)


class IdleReason(enum.StrEnum):
    """The public reason a terminal refusal surfaces; the only matchable shape.

    RETRY_PRESSURE parking is internal to ``__aenter__`` and is never a value
    here -- a caller only ever sees the terminal outcomes.
    """

    OVERSIZE = "oversize"
    STALE = "stale"


class Ratio(_Value):
    """A measured fraction in [0, 1]; refuses nan/inf."""

    __slots__ = ()

    def __init__(self, value: float) -> None:
        self.value = _finite_float(value, "Ratio", lo=0.0, hi=1.0)


class Scale(_Value):
    """An output multiplier in [0, 1]; the capacity equation's scale term."""

    __slots__ = ()

    def __init__(self, value: float) -> None:
        self.value = _finite_float(value, "Scale", lo=0.0, hi=1.0)


class Multiplier(_Value):
    """A growth multiplier >= 1.0 (the retype of ramp_multiplier)."""

    __slots__ = ()

    def __init__(self, value: float) -> None:
        self.value = _finite_float(value, "Multiplier", lo=1.0, hi=None)


class FloorDivisor(_Value):
    """A positive integer denominator >= 1 (the retype of floor_fraction)."""

    __slots__ = ()

    def __init__(self, value: int) -> None:
        self.value = _int_ge(value, "FloorDivisor", minimum=1, reject_bool=False)


class Duration(_Value):
    """Non-negative elapsed time in float seconds."""

    __slots__ = ()

    def __init__(self, value: float) -> None:
        self.value = _finite_float(value, "Duration", lo=0.0, hi=None)


class Bytes(_Value):
    """Non-negative byte quantity."""

    __slots__ = ()

    def __init__(self, value: int) -> None:
        self.value = _int_ge(value, "Bytes", minimum=0, reject_bool=True)


@dataclass(frozen=True, slots=True)
class Cost:
    """Per-unit resource price, explicitly in bytes. Single field."""

    memory_bytes: Bytes


class Count(_Value):
    """Non-negative integer count of units."""

    __slots__ = ()

    def __init__(self, value: int) -> None:
        self.value = _int_ge(value, "Count", minimum=0, reject_bool=True)


class Instant:
    """A monotonic copy of an injected clock's last read; never a wall time.

    Only :meth:`Clock.now` produces an :class:`Instant`; the raw ``_of`` door
    exists for the clock seam and value-construction tests.
    """

    __slots__ = ("value",)
    value: float

    def __new__(cls, *_args: Any) -> Instant:  # hidden: Instant(now) is refused
        raise TypeError("Instant is produced only by Clock.now(); call _of() via the seam")

    def __init__(self, *_args: Any) -> None:  # never reached (__new__ raises)
        pass

    @classmethod
    def _of(cls, now: float) -> Instant:
        self = super().__new__(cls)
        self.value = float(now)
        return self


class Progress:
    """The single progress record; ``last_advance >= last_decision`` by law."""

    __slots__ = ("last_advance", "last_decision")

    def __init__(self, *, last_advance: Instant, last_decision: Instant) -> None:
        if last_advance.value < last_decision.value:
            raise ValueError(
                "Progress requires last_advance >= last_decision: a granting decision is progress"
            )
        self.last_advance = last_advance
        self.last_decision = last_decision


class Limits:
    """One resource's target/hard ceiling pair; target <= hard enforced."""

    __slots__ = ("target", "hard")

    def __init__(self, *, target: Ratio, hard: Ratio) -> None:
        if target.value > hard.value:
            raise ValueError("Limits requires target <= hard: a hard ceiling below target is incoherent")
        self.target = target
        self.hard = hard


@dataclass(frozen=True)
class SensorBinding:
    """A per-resource (limits, gauge) binding inside a Sensing."""

    limits: Limits
    gauge: Any  # a ResourceGauge Protocol implementer, injected from the seam


class Sensing:
    """The sensor set, complete over {MEMORY, CPU}. One gauge+one limit per resource."""

    __slots__ = ("sensors",)

    def __init__(self, sensors: dict[ResourceId, SensorBinding]) -> None:
        if set(sensors) != {ResourceId.MEMORY, ResourceId.CPU}:
            raise ValueError(f"Sensing must be complete over {{MEMORY, CPU}}, got {set(sensors)}")
        self.sensors = sensors


@dataclass(frozen=True)
class AllocationEntry:
    """One lane's granted amount."""

    amount: Count


class Allocation:
    """A granted allocation, complete over {GENERAL, HEAVY}."""

    __slots__ = ("lanes", "total")

    def __init__(self, lanes: dict[LaneId, AllocationEntry]) -> None:
        if set(lanes) != {LaneId.GENERAL, LaneId.HEAVY}:
            raise ValueError(f"Allocation must be complete over {{GENERAL, HEAVY}}, got {set(lanes)}")
        self.lanes = lanes
        self.total = Count(lanes[LaneId.GENERAL].amount.value + lanes[LaneId.HEAVY].amount.value)

    def for_lane(self, lane: LaneId) -> Count:
        """That lane's granted amount."""
        return self.lanes[lane].amount


class Want:
    """A request for work, complete over {GENERAL, HEAVY} with a positive total."""

    __slots__ = ("lanes", "total")

    def __init__(self, lanes: dict[LaneId, Count]) -> None:
        if set(lanes) != {LaneId.GENERAL, LaneId.HEAVY}:
            raise ValueError(f"Want must be complete over {{GENERAL, HEAVY}}, got {set(lanes)}")
        total = lanes[LaneId.GENERAL].value + lanes[LaneId.HEAVY].value
        if total < 1:
            raise ValueError("Want must request a positive total; there is no empty Want")
        self.lanes = lanes
        self.total = Count(total)

    @classmethod
    def of(cls, *, general: Count, heavy: Count) -> Want:
        """Keyword, all-lane construction: the one supported door (¬ ``at_least``)."""
        return cls({LaneId.GENERAL: general, LaneId.HEAVY: heavy})


class MinAll:
    """The MIN_ALL scale source: the lane honours combined resource pressure."""

    __slots__ = ()


@dataclass(frozen=True, slots=True)
class RideCpuWhenFree:
    """The RIDE scale source: ride the CPU scale when headroom exceeds a threshold."""

    threshold: Ratio


ScaleSource = MinAll | RideCpuWhenFree


class Lease:
    """A live, committed reservation created only by a grant on a specific gate.

    ``release`` is synchronous and idempotent: the first call frees the
    amount; a wrong-gate release raises. The gate records the full per-lane map."""

    __slots__ = ("_lane", "_amount", "_gate", "_per_lane", "_cold", "_released")

    def __init__(self, *, lane: LaneId, amount: Count, gate: AdmissionGate) -> None:
        self._lane = lane
        self._amount = amount
        self._gate = gate
        self._per_lane = {lane: amount.value}
        self._cold = False
        self._released = False

    @classmethod
    def _of(cls, gate: AdmissionGate, per_lane: dict[LaneId, int], *, cold: bool = False) -> Lease:
        """The gate-only door: a lease carrying a multi-lane allocation."""
        positive = {lane: n for lane, n in per_lane.items() if n > 0}
        primary = next(iter(positive)) if positive else LaneId.GENERAL
        lease = cls(lane=primary, amount=Count(sum(per_lane.values())), gate=gate)
        lease._per_lane = dict(per_lane)
        lease._cold = cold
        return lease

    @property
    def lane(self) -> LaneId:
        return self._lane

    @property
    def amount(self) -> Count:
        return self._amount

    @property
    def gate(self) -> AdmissionGate:
        return self._gate

    def release(self, *, work_completed: bool = True) -> bool:
        """Return this reservation to its gate. True on the first release only."""
        if self._released:
            return False
        self._gate._release_lease(self, work_completed=work_completed)
        self._released = True
        return True


class Granted:
    """A live grant: positive allocation plus a committed lease."""

    __slots__ = ("allocation", "scale", "paced", "lease")

    def __init__(
        self,
        *,
        allocation: Allocation,
        scale: Scale,
        paced: bool,
        lease: Lease,
    ) -> None:
        if allocation.total.value < 1:
            raise ValueError("Granted requires allocation.total >= 1: a zero grant is unconstructible")
        if lease is None:
            raise ValueError("Granted requires a live lease: a grant without a reservation is a lie")
        self.allocation = allocation
        self.scale = scale
        self.paced = paced
        self.lease = lease
