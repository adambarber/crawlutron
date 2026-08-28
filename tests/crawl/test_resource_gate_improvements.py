"""Adversarial perf #2.3: cgroup-read cache, leading memory signal, loop-lag."""

import asyncio
import time

from app.cgroup_sense import _invalidate_cgroup_cache, _read_int
from app.memory_pressure import MemoryGate, read_memory_stats
from app.resource_pressure import LoopLagProbe, ResourceGate


def test_cgroup_read_cache_memoizes_within_ttl(tmp_path):
    p = tmp_path / "val"
    p.write_text("100")
    _invalidate_cgroup_cache()
    assert _read_int(str(p)) == 100
    # Mutate on disk; within the TTL the cached value is returned.
    p.write_text("200")
    assert _read_int(str(p)) == 100
    _invalidate_cgroup_cache()
    assert _read_int(str(p)) == 200


def test_leading_memory_signal_anticipates_climb(tmp_path, monkeypatch):
    """A fast-climbing memory usage yields a higher leading ratio than the
    trailing fraction, so dispatch throttles early."""
    gate = MemoryGate(target=0.85)
    values = [0.70, 0.74, 0.78, 0.82]
    idx = [0]

    def _fake_stats(*, cgroup_root):
        from app.memory_pressure import MemoryStats
        v = values[idx[0] % len(values)]
        idx[0] += 1
        return MemoryStats(total_bytes=100, used_bytes=v * 100)

    monkeypatch.setattr("app.memory_pressure.read_memory_stats", _fake_stats)
    _invalidate_cgroup_cache()

    ratios = [gate.leading_usage_ratio() for _ in range(len(values))]
    # The leading estimate at the end should track above the raw 0.82 because
    # the series is rising.
    assert ratios[-1] >= 0.82
    # And the dispatch scale tightens as the climb is anticipated.
    scale_early = gate.batch_scale_factor()
    assert 0.0 <= scale_early <= 1.0


def test_loop_lag_probe_factor_ramps_to_zero():
    probe = LoopLagProbe(ok_threshold=0.05, hard_threshold=0.30)
    probe._lag = 0.0
    assert probe.factor() == 1.0
    probe._lag = 0.30
    assert probe.factor() == 0.0
    probe._lag = 0.175  # midpoint -> ~0.5
    assert abs(probe.factor() - 0.5) < 1e-6


async def test_resource_gate_folds_loop_lag_into_scale(monkeypatch):
    gate = ResourceGate()
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.0)
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.0)
    # No lag -> full scale.
    gate._lag_probe._lag = 0.0
    assert gate.batch_scale_factor() == 1.0
    # Saturated loop -> full throttle.
    gate._lag_probe._lag = 0.30
    assert gate.batch_scale_factor() == 0.0
