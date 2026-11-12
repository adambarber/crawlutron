"""Saturated admission never silently returns (0,0): the floor law.

A saturated box must grant a floor (>= 1) or refuse OVERSIZE inside the admit,
never yield a silent zero that makes the recursive loop spin.
"""

from __future__ import annotations

from admission import Count, Want
from admission.errors import AdmissionRefused
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import (
    TaskRamBudget,
    build_admission_gate,
    build_admission_policy,
)


def _saturated_policy():
    return build_admission_policy(
        3 * 1024 * 1024 * 1024,
        pool_size=2,
        max_concurrent_pages=3,
        budget=TaskRamBudget(),
    )


async def test_saturated_batch_grants_a_floor_or_refuses_never_zero():
    """A saturated want yields a floor or an OVERSIZE refusal, never (0,0).
    The kernel admit parks internally and grants floor >= 1 or refuses OVERSIZE;
    both are progress -- never a silent zero that makes the loop hot-spin."""
    gate = ResourceGate(memory_ratio=lambda: 0.99, cpu_ratio=lambda: 0.99)
    gate.memory.target = 0.85
    gate.memory.hard = 0.90
    gate.cpu.target = 0.90
    gate.cpu.hard = 0.95
    admission = build_admission_gate(gate, _saturated_policy(), TaskRamBudget())
    want = Want.of(general=Count(2), heavy=Count(0))
    granted_total: int | None = None
    try:
        async with admission.admit(want) as granted:
            granted_total = granted.allocation.total.value
    except AdmissionRefused:
        granted_total = None  # OVERSIZE terminal: never a silent (0,0)
    assert granted_total is None or granted_total >= 1, (
        "saturated admission granted 0 -- the (0,0)-spin must be dead"
    )
