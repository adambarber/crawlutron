"""Adversarial sensing input: a raising gauge, and the free==cost boundary.

The gauge doctrine (7) fail-closes a raising sensor to hard-saturated (usage=1),
and the capacity law is exact at the cost boundary. Each test names its mutant.
"""

from __future__ import annotations

import dataclasses
import logging

import pytest

from admission.values import Ratio
from tests._support._doors import AdmissionRefused, Count, IdleReason, LaneId, Want
from tests._support._gate_factory import GateSpec, build_gate


class _RaisingGauge:
    """A sensor that raises on read: the gate must fail closed, not spuriously admit."""

    def is_warm(self) -> bool:
        return True

    def last_sample_age(self, now: float) -> float:
        return 0.0

    def read_usage(self) -> Ratio:
        raise BrokenPipeError("sensor died")


async def _refuse_via_raising_cpu_gauge() -> IdleReason:
    """Drive a capacity want through a raising CPU gauge; return the refusal reason."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), cpu_gauge=_RaisingGauge(), stale_bound=0.05))
    with pytest.raises(AdmissionRefused) as exc:
        async with gate.admit(Want.of(general=Count(1), heavy=Count(0))):
            pass
    return exc.value.reason


async def test_raising_gauge_fails_closed_to_refusal() -> None:
    """A raising gauge reads as dead, so a capacity-tight want is refused, never admitted as free."""
    # Mutant: swallowing the gauge error as free would admit against a dead sensor.
    assert await _refuse_via_raising_cpu_gauge() is IdleReason.STALE


async def test_raising_gauge_failure_surfaces_a_log_naming_the_gauge(caplog) -> None:
    """A dead gauge is never silent: the admit path logs a warning naming the
    failing resource while still failing closed (the gate keeps running stale)."""
    with caplog.at_level(logging.WARNING, logger="admission.gate"):
        reason = await _refuse_via_raising_cpu_gauge()
        # Mutant: an empty/raising `_gauge_failure` never emits this signal, so
        # the operator has no idea a pressure sensor died.
        warning = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warning, "a raising gauge must log a warning, not die mute"
        assert any("cpu" in r.getMessage() for r in warning), "the warning must name the failing gauge"
        assert reason is IdleReason.STALE


class _RaisingLogHandler(logging.Handler):
    """A logging handler whose emit raises: observability that must never break admission."""

    def emit(self, record: logging.LogRecord) -> None:
        raise RuntimeError("log sink died")


async def test_a_raising_gauge_reporter_never_breaks_admission() -> None:
    """Even if the gauge-failure reporter itself raises (a hostile log sink), the
    gate still fail-closes: hard-saturation is applied, nothing crashes the admit."""
    logger = logging.getLogger("admission.gate")
    handler = _RaisingLogHandler()
    logger.addHandler(handler)
    try:
        # Mutant: letting the reporter raise would escape the except and crash the
        # decision path (the gate would throw out of `admit` instead of refusing STALE).
        assert await _refuse_via_raising_cpu_gauge() is IdleReason.STALE
    finally:
        logger.removeHandler(handler)


async def test_free_exactly_equals_cost_grants_one() -> None:
    """free == cost grants exactly one HEAVY unit (the == boundary is granted-side)."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), heavy_cost_mib=700, memory_budget_mib=700))
    cm = gate.admit(Want.of(general=Count(0), heavy=Count(1)))
    granted = await cm.__aenter__()
    assert granted.allocation.for_lane(LaneId.HEAVY).value == 1
    granted.lease.release()
    await cm.__aexit__(None, None, None)


async def test_free_one_byte_below_cost_raises_oversize() -> None:
    """free == cost - 1 refuses: the OVERSIZE boundary is the byte below cost."""
    gate, _ = build_gate(dataclasses.replace(GateSpec(), heavy_cost_mib=7, memory_budget_mib=6))
    with pytest.raises(AdmissionRefused) as exc:
        async with gate.admit(Want.of(general=Count(0), heavy=Count(1))):
            pass
    # Mutant: granting a byte short of a full unit would over-commit memory.
    assert exc.value.reason is IdleReason.OVERSIZE


async def test_non_monotonic_clock_never_moves_progress_backwards() -> None:
    """Progress never travels backward even if a foreign clock dips below the last decision."""
    gate, handles = build_gate()
    handles.now = 100.0
    cm = gate.admit(Want.of(general=Count(1), heavy=Count(0)))
    granted = await cm.__aenter__()
    handles.now = 5.0  # a non-monotonic clock dips below the last decision
    granted.lease.release(work_completed=True)
    await cm.__aexit__(None, None, None)
    # Mutant: trusting the dipped read would move last_advance backwards.
    assert gate.progress().last_advance.value >= 100.0
