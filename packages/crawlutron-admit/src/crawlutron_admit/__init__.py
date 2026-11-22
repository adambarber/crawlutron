"""Resource-adaptive paced-admission kernel (distinct from ``crawlutron/admission`` URL admission).

Zero ``crawlutron.*`` imports, zero I/O, a closed typed surface; callers own frontier
queueing. Door: ``async with gate.admit(want) as granted:`` yields a live :class:`Granted`.
"""

from __future__ import annotations

from crawlutron_admit.errors import AdmissionRefused, NestedAdmitError, WrongGateError
from crawlutron_admit.gate import AdmissionGate, Decision, DecisionKind, StallGuard
from crawlutron_admit.gauge import GaugeState, ResourceGauge
from crawlutron_admit.policy import LaneSpec, PacingPolicy
from crawlutron_admit.seams import Clock, Signal
from crawlutron_admit.values import (
    Allocation,
    AllocationEntry,
    Bytes,
    Cost,
    Count,
    Duration,
    FloorDivisor,
    Granted,
    IdleReason,
    Instant,
    LaneId,
    Lease,
    Limits,
    MinAll,
    Multiplier,
    Progress,
    Ratio,
    ResourceId,
    RideCpuWhenFree,
    Scale,
    ScaleSource,
    Sensing,
    SensorBinding,
    Want,
)

__all__ = [
    "AdmissionGate",
    "AdmissionRefused",
    "Allocation",
    "AllocationEntry",
    "Bytes",
    "Clock",
    "Cost",
    "Count",
    "Decision",
    "DecisionKind",
    "Duration",
    "FloorDivisor",
    "GaugeState",
    "Granted",
    "IdleReason",
    "Instant",
    "LaneId",
    "LaneSpec",
    "Lease",
    "Limits",
    "MinAll",
    "Multiplier",
    "NestedAdmitError",
    "PacingPolicy",
    "Progress",
    "Ratio",
    "ResourceGauge",
    "ResourceId",
    "RideCpuWhenFree",
    "Scale",
    "ScaleSource",
    "SensorBinding",
    "Sensing",
    "Signal",
    "StallGuard",
    "Want",
    "WrongGateError",
]
