"""Inventory of the admission doubles (row 9): every call site is a pure
FakeResourceGauge. 13 migrated PinnedGate sites plus the ContentionFreeGate
harness; 4 pinned_resource_gate factories deferred to C2 on the real gate."""

from __future__ import annotations

import tests._admission_doubles as doubles
from crawlutron.runtime.pressure_gates import ResourceGate
from tests._admission_doubles import pinned_resource_gate


def test_admission_doubles_inventory(monkeypatch):
    """13 migrated PinnedGate sites -> FakeResourceGauge; ContentionFreeGate migrated;
    4 pinned_resource_gate factories deferred to C2 on the real ResourceGate."""
    # PinnedGate is removed: any site still importing/constructing it reds here.
    assert not hasattr(doubles, "PinnedGate"), "PinnedGate must not survive the migration"

    # The replacement double: structural ResourceGate double, no production base, real init.
    fake_gauge = doubles.FakeResourceGauge
    assert not issubclass(fake_gauge, ResourceGate), "FakeResourceGauge must not subclass ResourceGate"

    # ContentionFreeGate migrated to the FakeResourceGauge shape.
    from tests._recursive_harnesses import ContentionFreeGate

    assert issubclass(ContentionFreeGate, fake_gauge), "ContentionFreeGate must implement FakeResourceGauge"
    # The full protocol surface answers: a skipped __init__ leaves self.calls unset and a later
    # http_scale_factor read would raise. Each migrated double answers every scale/ratio method.
    gate = ContentionFreeGate(1.0)
    assert (gate.batch_scale_factor(), gate.http_scale_factor(), gate.memory_ratio(), gate.cpu_ratio()) == (
        1.0,
        1.0,
        0.0,
        0.0,
    ), "ContentionFreeGate must answer the full gauge protocol"

    # Deferred to C2: pinned_resource_gate still hands out a real ResourceGate.
    gate, _waited = pinned_resource_gate(monkeypatch, cpu_ratio=lambda: 0.0, memory_ratio=lambda: 0.0)
    assert isinstance(gate, ResourceGate), "deferred pinned_resource_gate must remain a real ResourceGate"
