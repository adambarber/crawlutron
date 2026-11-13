"""Construction is admission: every value object refuses ill-formed spellings at build.

Refusal-shaped spelling is the NormalizedUrl bar -- illegal state fails at construction,
never silently. Each test names the mutant it kills.
"""

from __future__ import annotations

import math

import pytest

from admission import (
    Allocation,
    AllocationEntry,
    Bytes,
    Cost,
    Count,
    Duration,
    FloorDivisor,
    Granted,
    Instant,
    LaneId,
    Lease,
    Limits,
    Multiplier,
    Progress,
    Ratio,
    ResourceId,
    Scale,
    Sensing,
    Want,
)


def test_floor_divisor_rejects_zero_and_negative() -> None:
    """FloorDivisor(0) is a construction error: it would divide by zero."""
    with pytest.raises(ValueError):
        FloorDivisor(0)
    with pytest.raises(ValueError):
        FloorDivisor(-1)
    # >= 1 is the valid floor; a swept 1 must construct.
    assert FloorDivisor(1).value == 1


def test_multiplier_rejects_below_one() -> None:
    """Multiplier is the retype of ramp_multiplier: a value < 1 would shrink the ramp."""
    with pytest.raises(ValueError):
        Multiplier(0.5)
    with pytest.raises(ValueError):
        Multiplier(0.0)
    assert Multiplier(1.0).value == 1.0
    assert Multiplier(1.10).value == pytest.approx(1.10)


def test_ratio_and_scale_reject_out_of_range_and_non_finite() -> None:
    """Ratio/Scale live in [0, 1] and refuse nan/inf -- a NaN scale gate is poison."""
    for cls in (Ratio, Scale):
        with pytest.raises(ValueError):
            cls(-0.01)
        with pytest.raises(ValueError):
            cls(1.01)
        with pytest.raises(ValueError):
            cls(math.nan)
        with pytest.raises(ValueError):
            cls(math.inf)
    assert Ratio(0.0).value == 0.0
    assert Scale(1.0).value == 1.0


def test_scalar_refusals_at_zero_and_negative() -> None:
    """Bytes/Count/Duration refuse negatives; Duration is non-negative time."""
    with pytest.raises(ValueError):
        Bytes(-1)
    with pytest.raises(ValueError):
        Count(-1)
    with pytest.raises(ValueError):
        Duration(-0.1)
    assert Bytes(0).value == 0
    assert Count(0).value == 0
    assert Duration(0.0).value == 0.0


def test_cost_wraps_memory_bytes_and_rejects_negative() -> None:
    """Cost is priced in bytes; a negative byte price is a category error."""
    with pytest.raises(ValueError):
        Cost(Bytes(-1))
    assert Cost(Bytes(48 * 1024 * 1024)).memory_bytes.value == 48 * 1024 * 1024


def test_limits_refuses_swapped_target_hard_pair() -> None:
    """Limits(target > hard) refuses: a hard ceiling below target is incoherent."""
    with pytest.raises(ValueError):
        Limits(target=Ratio(0.9), hard=Ratio(0.5))
    pair = Limits(target=Ratio(0.5), hard=Ratio(0.9))
    assert pair.target.value == pytest.approx(0.5)
    assert pair.hard.value == pytest.approx(0.9)


def test_sensing_must_be_complete_over_memory_and_cpu() -> None:
    """Sensing completes over {MEMORY, CPU}: a missing lane is an under-sensed box."""
    from admission import SensorBinding

    mem_binding = SensorBinding(Limits(target=Ratio(0.2), hard=Ratio(0.8)), object())
    cpu_binding = SensorBinding(Limits(target=Ratio(0.1), hard=Ratio(0.9)), object())
    good = {ResourceId.MEMORY: mem_binding, ResourceId.CPU: cpu_binding}
    sensing = Sensing(good)
    assert set(sensing.sensors) == {ResourceId.MEMORY, ResourceId.CPU}
    with pytest.raises(ValueError):
        Sensing({ResourceId.MEMORY: mem_binding})


def test_allocation_must_be_complete_over_lane_id() -> None:
    """Allocation completes over {GENERAL, HEAVY}; a missing lane is an incoherent grant."""
    good = {LaneId.GENERAL: AllocationEntry(Count(3)), LaneId.HEAVY: AllocationEntry(Count(1))}
    allocation = Allocation(good)
    assert allocation.total == Count(4)
    with pytest.raises(ValueError):
        Allocation({LaneId.GENERAL: AllocationEntry(Count(3))})


def test_want_of_requires_all_lanes_and_rejects_empty() -> None:
    """Want.of is keyword-all-lane and refuses an empty total."""
    want = Want.of(general=Count(2), heavy=Count(1))
    assert want.total == Count(3)
    with pytest.raises(ValueError):
        Want.of(general=Count(0), heavy=Count(0))
    # A raw constructor incomplete over LaneId is refused too: no silent zero-fill.
    with pytest.raises(ValueError):
        Want({LaneId.GENERAL: Count(1)})


def test_granted_requires_positive_total_and_a_live_lease() -> None:
    """A zero-total Granted is a lie unconstructible by type."""
    from tests._support._gate_factory import build_gate

    gate, _handles = build_gate()
    lease = Lease._of(gate, {LaneId.GENERAL: 1}, cold=False, scale=Scale(1.0))
    bad_allocation = Allocation(
        {LaneId.GENERAL: AllocationEntry(Count(0)), LaneId.HEAVY: AllocationEntry(Count(0))}
    )
    with pytest.raises(ValueError):
        Granted(allocation=bad_allocation, scale=Scale(1.0), lease=lease)
    good_allocation = Allocation(
        {LaneId.GENERAL: AllocationEntry(Count(1)), LaneId.HEAVY: AllocationEntry(Count(0))}
    )
    granted = Granted(allocation=good_allocation, scale=Scale(1.0), lease=lease)
    assert granted.lease is lease


def test_progress_enforces_advance_after_decision() -> None:
    """Progress keeps last_advance >= last_decision: a granting decision is progress."""
    with pytest.raises(ValueError):
        Progress(last_advance=Instant._of(1.0), last_decision=Instant._of(2.0))
    record = Progress(last_advance=Instant._of(5.0), last_decision=Instant._of(3.0))
    assert record.last_advance.value >= record.last_decision.value


def test_instant_is_hidden_constructor_only() -> None:
    """Instant is built only by Clock.now(); the raw form is a private _of door."""
    with pytest.raises(TypeError):
        Instant(3.0)
    inst = Instant._of(3.0)
    assert inst.value == 3.0
