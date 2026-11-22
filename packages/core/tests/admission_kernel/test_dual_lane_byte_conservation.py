"""One grant must not commit more bytes than the budget holds (C3).

Both lanes draw from one free-bytes pool, so per-lane capacities computed
independently each believe the whole pool is theirs. Each test names its mutant.
"""

from __future__ import annotations

import dataclasses

from crawlutron_admit.values import Allocation
from tests._support._doors import Count, LaneId, Want
from tests._support._gate_factory import GateSpec, build_gate

_GENERAL_MIB = 48
_HEAVY_MIB = 700
_BUDGET_MIB = 800
_RAMP = 64


async def _grant(gate, want: Want) -> Allocation:
    async with gate.admit(want) as granted:
        return granted.allocation


def _committed_mib(allocation: Allocation) -> int:
    return (
        allocation.for_lane(LaneId.GENERAL).value * _GENERAL_MIB
        + allocation.for_lane(LaneId.HEAVY).value * _HEAVY_MIB
    )


def _dual_lane_gate():
    return build_gate(
        dataclasses.replace(
            GateSpec(),
            general_cost_mib=_GENERAL_MIB,
            heavy_cost_mib=_HEAVY_MIB,
            memory_budget_mib=_BUDGET_MIB,
            ramp_floor=_RAMP,
            ramp_max=_RAMP,
            initial_ramp=_RAMP,
        )
    )


async def _dual_allocation() -> Allocation:
    gate, _ = _dual_lane_gate()
    return await _grant(gate, Want.of(general=Count(8), heavy=Count(1)))


async def test_dual_lane_grant_never_commits_more_than_the_budget() -> None:
    """GENERAL + HEAVY bytes in one grant must fit the shared pool."""
    allocation = await _dual_allocation()
    # Mutant: per-lane capacities against one unreduced pool commit 8*48 + 700 = 1,084.
    assert _committed_mib(allocation) <= _BUDGET_MIB


async def test_the_heavy_lane_is_not_starved_by_a_pre_filled_general_lane() -> None:
    """Bytes go to the largest unit first, so a full GENERAL ask cannot consume HEAVY's room."""
    allocation = await _dual_allocation()
    # Mutant: allocating GENERAL first leaves 800 - 192 = 608 < 700 and HEAVY grants zero.
    assert allocation.for_lane(LaneId.HEAVY).value >= 1
    assert allocation.for_lane(LaneId.GENERAL).value >= 1
