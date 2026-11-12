"""The gate's configuration objects: pacing policy and per-lane specification.

Validated value objects consumed by :class:`~admission.gate.AdmissionGate`; the seam
produces ceilings/costs, never the kernel's internals.
"""

from __future__ import annotations

from dataclasses import dataclass

from admission.values import (
    Cost,
    Count,
    Duration,
    FloorDivisor,
    Multiplier,
    ScaleSource,
)

__all__ = ["LaneSpec", "PacingPolicy"]


@dataclass(frozen=True)
class LaneSpec:
    """One lane's ceiling, per-unit cost and scale source.

    ``ceiling`` is the hard concurrency cap; ``cost`` is the per-unit byte price;
    ``scale_source`` tracks resource pressure (MIN_ALL, or RIDE the CPU scale)."""

    ceiling: Count
    cost: Cost
    scale_source: ScaleSource


@dataclass(frozen=True)
class PacingPolicy:
    """The ramp/park posture of an :class:`~admission.gate.AdmissionGate`.

    ``floor_fraction`` is a positive integer denominator (the retype of the legacy
    ``saturated_dispatch_floor`` divisor); ``ramp_floor`` is the decay floor."""

    ramp_floor: Count
    ramp_max: Count
    ramp_multiplier: Multiplier
    floor_fraction: FloorDivisor
    ramp_growth_window: Count = Count(3)
    cold_probe: Count = Count(1)
    stale_bound: Duration = Duration(30.0)
    initial_ramp: Count | None = None
