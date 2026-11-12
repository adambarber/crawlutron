"""H2: the reserved memory budget is never admitted (reserved==budget leaves no room).

Free capacity = memory_budget - reserved - in_flight_bytes; a fully reserved budget
refuses even a want that would otherwise fit."""

from __future__ import annotations

import dataclasses

import pytest
from admission import AdmissionRefused, IdleReason

from tests.admission_kernel._doors import _GENERAL_ONE
from tests.admission_kernel._gate_factory import GateSpec, build_gate


async def test_reserved_memory_never_admitted() -> None:
    """With the budget fully reserved, a want that would otherwise fit is refused."""
    gate, _ = build_gate(
        dataclasses.replace(
            GateSpec(general_cost_mib=48, memory_budget_mib=48, reserved_mib=48),
        )
    )
    # Mutant (ignoring reserved): free_bytes stays 48MiB, the want grants instead.
    with pytest.raises(AdmissionRefused) as exc:
        await gate.admit(_GENERAL_ONE).__aenter__()
    assert exc.value.reason is IdleReason.OVERSIZE
