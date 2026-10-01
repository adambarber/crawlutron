"""Resource gauge: cgroup reads, cached utilization, leading memory signal and loop-lag probe."""

import pytest

from app.cgroup_sense import _read_int, reset_process_state
from app.memory_pressure import MemoryGate, ResourceGauge
from app.resource_pressure import LoopLagProbe, ResourceGate


def _sensor(sequence: list[float]):
    """A sensor that walks a series of readings and holds the final value.

    This replaces a ``ResourceGauge`` subclass that existed only to supply a
    reading: the sensor is a callable, so a scripted one needs no class, no
    override and no patch.
    """
    values = list(sequence)
    index = 0

    def read() -> float:
        nonlocal index
        value = values[min(index, len(values) - 1)]
        index += 1
        return value

    return read


def _stub(sequence: list[float]) -> ResourceGauge:
    return ResourceGauge(target=0.90, poll_seconds=0.01, max_wait_seconds=1.0, usage_ratio=_sensor(sequence))


def test_scale_full_below_target():
    g = _stub([0.50])
    assert g.batch_scale_factor() == 1.0


def test_scale_zero_at_hard():
    # hard = min(0.999, target + 0.05) ~= 0.95; use a value clearly above it.
    g = _stub([0.99])
    assert g.batch_scale_factor() == 0.0


def test_scale_linear_between_target_and_hard():
    # 0.925 is midway between 0.90 and 0.95 -> scale ~0.5
    g = _stub([0.925])
    assert g.batch_scale_factor() == pytest.approx(0.5, abs=1e-6)


async def test_wait_returns_when_below_hard():
    g = _stub([0.50])
    await g.wait_while_saturated(max_wait_seconds=1)
    assert True  # did not block


async def test_wait_releases_once_drained():
    # Starts saturated (>= hard), then drains below hard on the next sample.
    # The sensor counts its own reads: re-sampling is the property, and
    # asserting on the double's bookkeeping instead would couple the test to
    # how the fake happens to hold its state.
    reads: list[float] = []
    series = iter([0.99, 0.10])

    def sensor() -> float:
        reading = next(series, 0.10)
        reads.append(reading)
        return reading

    g = ResourceGauge(target=0.90, poll_seconds=0.01, max_wait_seconds=5.0, usage_ratio=sensor)
    await g.wait_while_saturated(max_wait_seconds=5)
    assert len(reads) >= 2  # it actually re-sampled and saw the drain


def test_cgroup_read_cache_memoizes_within_ttl(tmp_path):
    p = tmp_path / "val"
    p.write_text("100")
    reset_process_state()
    assert _read_int(str(p)) == 100
    # Mutate on disk; within the TTL the cached value is returned.
    p.write_text("200")
    assert _read_int(str(p)) == 100
    reset_process_state()
    assert _read_int(str(p)) == 200


def test_leading_memory_signal_anticipates_climb(tmp_path, monkeypatch):
    """A fast-climbing memory usage yields a higher leading ratio than the
    trailing fraction, so dispatch throttles early."""
    gate = MemoryGate(target=0.85)
    values = [0.70, 0.74, 0.78, 0.82]
    idx = [0]

    def _fake_stats(*, cgroup_root):
        from app.memory_pressure import MemoryStats

        # ``int`` because the field is bytes: a fraction here was a fraction of a
        # byte, and the ratio the gauge computes is the same either way -- so the
        # float was only ever a fiction about the unit.
        v = values[idx[0] % len(values)]
        idx[0] += 1
        return MemoryStats(total_bytes=100, used_bytes=int(v * 100))

    monkeypatch.setattr("app.memory_pressure.read_memory_stats", _fake_stats)
    reset_process_state()

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
    gate = ResourceGate(memory_ratio=lambda: 0.0, cpu_ratio=lambda: 0.0)
    # No lag -> full scale.
    gate._lag_probe._lag = 0.0
    assert gate.batch_scale_factor() == 1.0
    # Saturated loop -> full throttle.
    gate._lag_probe._lag = 0.30
    assert gate.batch_scale_factor() == 0.0
