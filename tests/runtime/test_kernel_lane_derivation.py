"""Seam-side derivation: the kernel lane inputs from the sizing policy (C3)."""

from admission import Count, LaneId, LaneSpec, MinAll
from admission.values import Bytes

from crawlutron.runtime.pressure_sizing import kernel_lane_specs
from tests.runtime._seam_sizing import seam_sizing_budget, seam_sizing_policy


def test_general_lane_ceiling_is_max_http_concurrency() -> None:
    """HTTP maps to GENERAL, ceilinged by the RAM-derived HTTP cap."""
    policy = seam_sizing_policy()
    lanes = kernel_lane_specs(policy, seam_sizing_budget())
    assert lanes[LaneId.GENERAL].ceiling == Count(policy.max_http_concurrency)
    assert isinstance(lanes[LaneId.GENERAL].scale_source, MinAll)


def test_heavy_lane_ceiling_is_max_browser_concurrency() -> None:
    """Browser maps to HEAVY, ceilinged by the browser-tier cap."""
    policy = seam_sizing_policy()
    lanes = kernel_lane_specs(policy, seam_sizing_budget())
    assert lanes[LaneId.HEAVY].ceiling == Count(policy.max_browser_concurrency)
    assert isinstance(lanes[LaneId.HEAVY], LaneSpec)


def test_costs_are_bytes_from_the_task_ram_budget() -> None:
    """GENERAL costs the HTTP task weight, HEAVY the browser task weight, in bytes."""
    budget = seam_sizing_budget()
    lanes = kernel_lane_specs(seam_sizing_policy(), budget)
    assert lanes[LaneId.GENERAL].cost.memory_bytes == Bytes(budget.http_task_bytes)
    assert lanes[LaneId.HEAVY].cost.memory_bytes == Bytes(budget.browser_task_bytes)
