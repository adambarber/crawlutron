"""The scale path polls sensors live: decay is skipped at full scale, lag folded.

M2: a full-scale grant's release re-reads no gauge for a no-op decay. L3: loop-lag
is a callable folded once per decide, so a later grant sees the current lag."""

from __future__ import annotations

import dataclasses

from admission import Ratio
from tests._support._doors import _finish, _hold_one
from tests._support._gate_factory import FakeGauge, GateSpec, build_gate


class _CountingGauge(FakeGauge):
    """A gauge that counts read_usage calls; count() reads the running total."""

    def __init__(self, usage: float) -> None:
        super().__init__(usage)
        self._calls = 0

    def read_usage(self) -> Ratio:
        self._calls += 1
        return super().read_usage()

    def count(self) -> int:
        return self._calls


async def test_full_scale_release_does_not_reread_gauges() -> None:
    """At full scale the release skips decay, so the gauges are read only at decide."""
    mem_g = _CountingGauge(0.2)
    cpu_g = _CountingGauge(0.2)
    gate, _ = build_gate(dataclasses.replace(GateSpec(), mem_gauge=mem_g, cpu_gauge=cpu_g))
    cm, granted = await _hold_one(gate)
    spent = mem_g.count() + cpu_g.count()
    granted.lease.release(work_completed=True)
    await _finish(cm)
    # Mutant (unconditional decay): the release re-reads both gauges and the
    # combined count climbs past the decide-time total.
    assert mem_g.count() + cpu_g.count() == spent


async def test_loop_lag_callable_polled_live() -> None:
    """Two sequential grants fold the current lag at each decide."""
    box = {"lag": 0.7}
    gate, _ = build_gate(dataclasses.replace(GateSpec(), loop_lag_fn=lambda: Ratio(box["lag"])))
    cm1, first = await _hold_one(gate)
    assert first.scale.value == 0.7
    await _finish(cm1)  # release the first grant before admitting the second
    box["lag"] = 1.0
    cm2, second = await _hold_one(gate)
    # Mutant (fold the initial lag into the scale once at construction): the second
    # grant also folds 0.7, not the live 1.0.
    assert second.scale.value == 1.0
    await _finish(cm2)
