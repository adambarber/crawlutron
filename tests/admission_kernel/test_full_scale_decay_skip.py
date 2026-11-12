"""M2: a full-scale grant's release does not re-read the sensors for decay.

At full scale (grant scale >= 1.0) the ramp did not decay during the lease, so the
release skips decay and leaves the sensor read count unchanged."""

from __future__ import annotations

import dataclasses

from admission import Ratio
from tests.admission_kernel._doors import _finish, _hold_one
from tests.admission_kernel._gate_factory import FakeGauge, GateSpec, build_gate


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
