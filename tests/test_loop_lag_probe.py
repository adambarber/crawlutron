"""M16: the loop-lag probe must start on a running loop.

ResourceGate is constructed at import time (engine module level) where no
event loop runs, so an __init__-time probe start silently never schedules
and lag reads 0.0 forever. Startup is explicit: lifespan calls
start_lag_probe() once a loop runs; construction alone never starts it.
"""

import app.resource_pressure as rp
from app.resource_pressure import LoopLagProbe, ResourceGate


def test_probe_does_not_start_without_running_loop():
    probe = LoopLagProbe()
    probe.start()
    assert probe._task is None
    assert probe.get() == 0.0


def test_loop_lag_probe_flag_accepts_shared_bool_spellings(monkeypatch):
    import importlib

    for value in (" true ", "yes", "1"):
        monkeypatch.setenv("BROWSER_CRAWLER_LOOP_LAG_PROBE", value)
        module = importlib.reload(rp)
        assert module._LOOP_LAG_PROBE_ENABLED is True
    monkeypatch.delenv("BROWSER_CRAWLER_LOOP_LAG_PROBE")
    importlib.reload(rp)


def test_gate_construction_never_starts_probe(monkeypatch):
    monkeypatch.setattr(rp, "_LOOP_LAG_PROBE_ENABLED", True)
    gate = ResourceGate()
    assert gate._lag_probe._task is None


async def test_gate_probe_starts_explicitly_on_running_loop(monkeypatch):
    monkeypatch.setattr(rp, "_LOOP_LAG_PROBE_ENABLED", True)
    gate = ResourceGate()
    gate.start_lag_probe()
    try:
        task = gate._lag_probe._task
        assert task is not None and not task.done()
        # Idempotent: a second start reuses the running task.
        gate.start_lag_probe()
        assert gate._lag_probe._task is task
    finally:
        gate.close()
    assert gate._lag_probe._task is None
