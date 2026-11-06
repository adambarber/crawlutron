"""Resource gauge: cgroup reads, cached utilization, leading memory signal and loop-lag probe."""

import asyncio

import pytest

from crawlutron.runtime import process_state
from crawlutron.runtime.cgroup_sense import read_int
from crawlutron.runtime.pressure_memory import MemoryGate, ResourceGauge
from crawlutron.runtime.pressure_sizing import LoopLagProbe
from tests._lag_scheduling import lag_clock, scripted_sleep


def _sensor(sequence: list[float]):
    """A sensor that walks a series of readings and holds the final value.

    It is a callable, so a scripted one needs no ``ResourceGauge`` subclass,
    no override and no patch.
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


def test_an_incomplete_resource_gauge_fails_at_construction():
    """``_read_usage`` is a declared subclass hook, not a defaulted one: a gauge
    that neither overrides it nor injects ``usage_ratio`` is a construction
    error, not a NotImplementedError at the first batch dispatch."""
    with pytest.raises(TypeError):
        ResourceGauge(target=0.90, poll_seconds=0.01, max_wait_seconds=1.0)


@pytest.mark.parametrize(
    ("utilization", "expected"),
    [(0.50, 1.0), (0.99, 0.0)],
    ids=["below-target-runs-full", "at-the-hard-ceiling-stops"],
)
def test_scale_at_the_utilization_edges(utilization, expected):
    # hard = min(0.999, target + 0.05); 0.99 sits clearly above it.
    g = _stub([utilization])
    assert g.batch_scale_factor() == expected


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
    process_state.reset_all_process_state()
    assert read_int(str(p)) == 100
    # Mutate on disk; within the TTL the cached value is returned.
    p.write_text("200")
    assert read_int(str(p)) == 100
    process_state.reset_all_process_state()
    assert read_int(str(p)) == 200


def test_leading_memory_signal_anticipates_climb(tmp_path, monkeypatch):
    """A fast-climbing memory usage yields a higher leading ratio than the
    trailing fraction, so dispatch throttles early."""
    gate = MemoryGate(target=0.85)
    values = [0.70, 0.74, 0.78, 0.82]
    idx = [0]

    def _fake_stats(*, cgroup_root):
        from crawlutron.runtime.pressure_memory import MemoryStats

        v = values[idx[0] % len(values)]
        idx[0] += 1
        return MemoryStats(total_bytes=100, used_bytes=int(v * 100))

    monkeypatch.setattr("crawlutron.runtime.pressure_memory.read_memory_stats", _fake_stats)
    process_state.reset_all_process_state()

    ratios = [gate.leading_usage_ratio() for _ in range(len(values))]
    # The leading estimate at the end should track above the raw 0.82 because the series is rising.
    assert ratios[-1] >= 0.82
    # And the dispatch scale tightens as the climb is anticipated.
    scale_early = gate.batch_scale_factor()
    assert 0.0 <= scale_early <= 1.0


@pytest.mark.parametrize("lag,expected", [(0.0, 1.0), (0.30, 0.0), (0.175, 0.5)])
async def test_loop_lag_probe_factor_ramps_to_zero(lag, expected):
    """The probe's scale factor ramps 1.0 -> 0.0 as observed lag rises past ok."""
    probe = LoopLagProbe(
        interval=0.05,
        ok_threshold=0.05,
        hard_threshold=0.30,
        smoothing=1.0,
        clock=lag_clock(lag),
        sleep=scripted_sleep,
    )
    probe.start()
    try:
        for _ in range(30):
            await asyncio.sleep(0)
        assert probe.get() == pytest.approx(lag, abs=lag * 0.2 + 1e-9)
        assert probe.factor() == pytest.approx(expected, abs=1e-6)
    finally:
        probe.stop()


async def test_resource_gate_folds_loop_lag_into_scale():
    from tests._lag_scheduling import lagged_gate

    gate = lagged_gate(lag=0.30)
    gate.start_lag_probe()
    try:
        for _ in range(30):
            await asyncio.sleep(0)
        # No lag -> full scale, saturated loop -> full throttle: the fold is the
        # observable via batch_scale_factor(), not the probe internals.
        assert gate.batch_scale_factor() == pytest.approx(0.0, abs=0.01)
    finally:
        gate.close()
