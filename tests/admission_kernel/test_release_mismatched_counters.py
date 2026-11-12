"""B2: releasing a lease whose counters disagree with the gate raises, not returns.

A silent mid-loop return half-releases a lane, leaves the version unbumped and
starves parked waiters; a mismatch is corruption and must raise."""

from __future__ import annotations

import pytest
from admission import LaneId
from admission.values import Lease

from tests.admission_kernel._doors import _finish, _hold_one
from tests.admission_kernel._gate_factory import build_gate

_MISMATCHED = {LaneId.GENERAL: 10}  # ten units against a gate holding zero


async def test_release_with_mismatched_counters_raises_and_signals_never_silently_returns() -> None:
    """A mismatched release raises, leaves progress untouched, and frees nothing."""
    gate, _ = build_gate()
    before = gate.progress().last_decision.value
    lease = Lease._of(gate, _MISMATCHED, cold=False)
    with pytest.raises(AssertionError):
        lease.release(work_completed=True)
    # Mutant (silent return): the release "succeeds" and half-releases; the
    # follow-up grant below must still fit, proving the raise path left none.
    assert gate.progress().last_decision.value >= before
    cm, granted = await _hold_one(gate)
    granted.lease.release(work_completed=True)
    await _finish(cm)
