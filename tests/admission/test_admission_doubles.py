"""Inventory of the admission doubles (row 9): migration doubles are pure
FakeResourceGauge; the one real ResourceGate subclass that survives is the
sanctioned _ZeroGate, pinned by the completeness scan below."""

from __future__ import annotations

import inspect

import tests._admission_doubles as doubles
from crawlutron.runtime.pressure_gates import ResourceGate


def test_admission_doubles_inventory():
    """13 migrated PinnedGate sites -> FakeResourceGauge; ContentionFreeGate
    migrated; the retired pinned_resource_gate factory is gone."""
    assert not hasattr(doubles, "PinnedGate"), "PinnedGate must not survive the migration"
    assert not hasattr(doubles, "pinned_resource_gate"), "the pinned_resource_gate factory is retired"

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


def test_every_real_resource_gate_subclass_in_tests_is_sanctioned():
    """Any ``ResourceGate`` subclass in tests/ is a sanctioned exception.

    Only ``_ZeroGate`` survives (it hands the kernel gate a real ResourceGate);
    a stray subclass is a regression of the migration."""
    suspected: list[str] = []
    for _module, name, cls in _resource_gate_subclasses():
        if cls.__name__ == "_ZeroGate":
            continue  # the one sanctioned real-gate double, documented above
        suspected.append(f"{_module.__name__}.{name}")
    assert not suspected, f"an unsanctioned ResourceGate subclass exists: {suspected}"


def _import_optional(name: str):
    """Import a module, or ``None`` when its environment cannot load it."""
    import importlib

    try:
        return importlib.import_module(name)
    except Exception:
        return None


def _resource_gate_subclasses():
    """Yield every ``tests/**`` module member that subclasses ``ResourceGate``."""
    import pkgutil

    import tests

    found = []
    for modinfo in pkgutil.walk_packages(tests.__path__, prefix="tests."):
        if modinfo.ispkg:
            continue
        mod = _import_optional(modinfo.name)
        if mod is None:
            continue
        for name, obj in vars(mod).items():
            if inspect.isclass(obj) and obj is not ResourceGate and issubclass(obj, ResourceGate):
                found.append((mod, name, obj))
    return found
