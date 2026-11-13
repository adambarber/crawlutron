"""Admission control: CPU and memory gates, weighted strategy admission and page-budget bounds."""

from __future__ import annotations

import asyncio

import pytest

from admission import AdmissionRefused, Count, IdleReason, LaneId, Want
from crawlutron.health.readiness import ReadinessState
from crawlutron.runtime.metrics import get_metrics
from crawlutron.runtime.pressure_memory import MemoryStats, read_memory_stats
from crawlutron.runtime.pressure_sizing import CpuGate, ResourceGate, read_cpu_quota, read_cpu_usage_seconds
from tests._support._doors import _GENERAL_ONE, _finish, _live
from tests._support._gate_factory import GateSpec, build_gate


async def _hold_in_task(gate):
    """Acquire a GENERAL grant inside its own task, keeping this context's lease
    marker clear so a sibling admit elsewhere is not a false nested admit."""
    return await asyncio.wait_for(asyncio.create_task(_live(gate, _GENERAL_ONE)), 1.0)


class TestReadinessState:
    def test_initial_state_is_ready(self):
        state = ReadinessState()
        snap = state.snapshot()
        assert snap.status == "ready"
        assert snap.active_requests == 0
        assert snap.saturated is False

    def test_begin_end_request(self):
        state = ReadinessState()
        assert state.begin_request() is True
        snap = state.snapshot()
        assert snap.active_requests == 1
        assert snap.saturated is False
        state.end_request()
        snap = state.snapshot()
        assert snap.active_requests == 0

    def test_fixed_cap_is_reported_and_refuses_at_the_bound(self):
        """A configured cap is the readiness report's real bound, reached when
        active requests sit AT it: refused (False) without taking a slot."""
        state = ReadinessState(max_active_requests=2)
        assert state.snapshot().max_active_requests == 2
        assert state.begin_request() and state.begin_request()
        assert state.snapshot().active_requests == 2
        assert state.begin_request() is False  # at the cap: refused, no slot taken
        assert state.snapshot().active_requests == 2

    def test_zero_cap_is_off(self):
        """0/None both mean unbounded (optional-presence): never refuses."""
        for cap in (None, 0):
            state = ReadinessState(max_active_requests=cap)
            assert state.snapshot().max_active_requests == 0
            assert state.begin_request() is True

    # Each gate rides at its own target (memory 0.92, cpu 0.90); the values below are over-the-line readings,
    # and they are the only data the property differs on.
    @pytest.mark.parametrize(
        ("gauge", "over_the_line"),
        [("resource_memory_utilization", 0.95), ("resource_cpu_utilization", 0.93)],
    )
    def test_saturated_when_a_gate_is_over_its_line(self, gauge, over_the_line):
        """Both gates carry the same shape: ready below the line, saturated above it.

        Two gauges in one test because the shape is the property; written per
        gate it was twice the walk, and the CPU copy had dropped the status.
        """
        metrics = get_metrics()
        metrics.resource_memory_utilization = 0.0
        metrics.resource_cpu_utilization = 0.0
        state = ReadinessState()
        assert state.snapshot().saturated is False
        setattr(metrics, gauge, over_the_line)
        snap = state.snapshot()
        assert snap.saturated is True
        assert snap.status == "saturated"

    def test_end_request_does_not_go_negative(self):
        state = ReadinessState()
        state.end_request()
        snap = state.snapshot()
        assert snap.active_requests == 0


# --- sensing: quota ---


@pytest.mark.parametrize(
    ("cpu_max", "expected"),
    [("200000 100000", 2.0), ("max 100000", None)],
    ids=["two-cpus", "max-is-unbounded"],
)
def test_read_cpu_quota_v2(tmp_path, cpu_max, expected):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "cpu.max").write_text(f"{cpu_max}\n")
    assert read_cpu_quota(cgroup_root=str(cg)) == expected


@pytest.mark.parametrize(
    ("quota_us", "expected"),
    [("150000", 1.5), ("-1", None)],
    ids=["one-and-a-half-cpus", "unlimited"],
)
def test_read_cpu_quota_v1(tmp_path, quota_us, expected):
    cg = tmp_path / "cgroup" / "cpu"
    cg.mkdir(parents=True)
    (cg / "cpu.cfs_quota_us").write_text(f"{quota_us}\n")
    (cg / "cpu.cfs_period_us").write_text("100000\n")
    assert read_cpu_quota(cgroup_root=str(tmp_path / "cgroup")) == expected


def test_read_cpu_quota_missing_is_unbounded(tmp_path):
    assert read_cpu_quota(cgroup_root=str(tmp_path / "does-not-exist")) is None


# --- sensing: usage ---


def test_read_cpu_usage_v2(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "cpu.stat").write_text("usage_usec 5000000\nuser_usec 3000000\nsystem_usec 2000000\n")
    assert read_cpu_usage_seconds(cgroup_root=str(cg)) == 5.0


def test_read_cpu_usage_v1(tmp_path):
    cg = tmp_path / "cgroup" / "cpuacct"
    cg.mkdir(parents=True)
    (cg / "cpuacct.usage").write_text("5000000000\n")  # 5 CPU-seconds
    assert read_cpu_usage_seconds(cgroup_root=str(tmp_path / "cgroup")) == 5.0


def test_read_cpu_usage_missing_is_none(tmp_path):
    assert read_cpu_usage_seconds(cgroup_root=str(tmp_path / "nope")) is None


# --- governance: rate sampling ---


def _cpu_gate(monkeypatch, *, target=0.90):
    """A CpuGate on a fake clock and a fake usage series, with a way to advance both.

    Tests once re-spelled the same two one-element lists; the gate stays real,
    only its two sensors are replaced, so the arithmetic is production's.
    """
    clock, usage = [0.0], [0.0]
    monkeypatch.setattr("crawlutron.runtime.pressure_gates.read_cpu_usage_seconds", lambda root="": usage[0])
    monkeypatch.setattr("crawlutron.runtime.pressure_gates.read_cpu_quota", lambda root="": 2.0)
    return CpuGate(target=target, clock=lambda: clock[0]), clock, usage


def test_cpu_gate_sampling_paces_by_the_injected_clock(monkeypatch):
    """#581: the CPU-rate sampling window reads the injected clock, not a hard
    monotonic() -- advancing the clock alone (no monkeypatch of time.monotonic)
    must age the sample window."""
    gate, clock, usage = _cpu_gate(monkeypatch)
    gate.batch_scale_factor()  # initialize sample at clock 0.0
    clock[0], usage[0] = 0.5, 0.6  # 0.6 CPU-seconds over 0.5 wall-seconds on 2 CPUs -> 0.6 utilization
    assert gate.cpu_ratio() == 0.6, (
        "the sampling window ignored the injected clock: the smoothed ratio never moved "
        "even though the fake clock crossed min_window_seconds"
    )


@pytest.mark.parametrize(
    ("cpu_seconds", "expected"),
    [(1.0, 1.0), (2.0, 0.0)],
    ids=["half-utilization-below-target", "at-the-hard-ceiling"],
)
def test_cpu_gate_at_the_utilization_edges(monkeypatch, cpu_seconds, expected):
    gate, clock, usage = _cpu_gate(monkeypatch)
    assert gate.batch_scale_factor() == 1.0  # first sight -> 0 utilization
    clock[0], usage[0] = 1.0, cpu_seconds
    assert gate.batch_scale_factor() == expected


def test_cpu_gate_scales_between_target_and_hard(monkeypatch):
    gate, clock, usage = _cpu_gate(monkeypatch)
    gate.batch_scale_factor()  # initialize sample
    # 1.85 CPU-seconds over 1.0 wall-second on 2 CPUs -> 0.925 util (mid soft zone)
    clock[0], usage[0] = 1.0, 1.85
    s = gate.batch_scale_factor()
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


async def test_cpu_gate_unbounded_is_noop(monkeypatch):
    """No cgroup quota and no manual budget -> never throttles."""
    gate = CpuGate(target=0.90)
    monkeypatch.setattr("crawlutron.runtime.pressure_gates.read_cpu_quota", lambda root="": None)
    monkeypatch.setattr("crawlutron.runtime.pressure_gates.read_cpu_usage_seconds", lambda root="": 999.0)
    assert gate.batch_scale_factor() == 1.0


# --- combined ResourceGate ---


async def test_admit_parks_while_capacity_consumed_and_releases():
    """Kernel-door: an admit that cannot fit a wanted lane parks internally and
    returns a grant once a release frees capacity -- the absorbed wait contract."""
    gate, _ = build_gate(GateSpec(general_ceiling=1, memory_usage=0.2, cpu_usage=0.2))
    cm, first = await _hold_in_task(gate)
    parked = asyncio.create_task(_live(gate, _GENERAL_ONE))
    await asyncio.sleep(0.02)
    assert parked.done() is False, "a saturated want returned without parking"
    first.lease.release()  # a release wakes the parked admit to re-decide
    await _finish(cm)
    parker_cm, parker_grant = await asyncio.wait_for(parked, 1.0)
    await _finish(parker_cm)
    assert parker_grant.allocation.for_lane(LaneId.GENERAL).value == 1


async def test_admit_returns_immediately_when_idle():
    """Kernel-door: with capacity free, admit returns without parking."""
    gate, _ = build_gate(GateSpec(general_ceiling=8, memory_usage=0.2, cpu_usage=0.2))
    cm, granted = await asyncio.wait_for(_live(gate, _GENERAL_ONE), 1.0)
    assert granted.allocation.for_lane(LaneId.GENERAL).value >= 1
    await _finish(cm)


async def test_oversize_want_refuses_bounded_rather_than_spinning():
    """Kernel-door: a want nothing can fit raises OVERSIZE immediately -- the
    bounded terminal that replaces the old (0,0)-and-spin wait."""
    gate, _ = build_gate(
        GateSpec(general_ceiling=8, heavy_ceiling=8, heavy_cost_mib=700, memory_budget_mib=50)
    )
    # A HEAVY want beyond the 50MiB budget can never fit: an immediate OVERSIZE.
    with pytest.raises(AdmissionRefused) as exc:
        await asyncio.wait_for(_live(gate, Want.of(general=Count(0), heavy=Count(1))), 1.0)
    assert exc.value.reason is IdleReason.OVERSIZE


def test_resource_gate_scale_is_min_of_resources(monkeypatch):
    # One holder per resource: the gate reads its sensors through the public `usage_ratio` seam, so flipping a
    # reading is writing a number, not re-patching a private method six times.
    memory, cpu = [0.99], [0.0]
    gate = ResourceGate(memory_ratio=lambda: memory[0], cpu_ratio=lambda: cpu[0])
    # Memory at 99% -> scale 0; CPU idle -> scale 1. Combined must be 0.
    assert gate.batch_scale_factor() == 0.0
    # Flip: CPU saturated, memory idle -> still 0.
    memory[0], cpu[0] = 0.0, 0.99
    assert gate.batch_scale_factor() == 0.0
    # Both idle -> full speed.
    memory[0], cpu[0] = 0.0, 0.0
    assert gate.batch_scale_factor() == 1.0


def test_metrics_expose_resource_utilization_and_counters():
    text = get_metrics().as_prometheus()
    assert "crawler_resource_memory_utilization" in text
    assert "crawler_resource_cpu_utilization" in text
    assert "crawler_resource_throttle_batches_total" in text
    # Defaults render without error.
    assert "crawler_resource_memory_utilization 0.0000" in text
    assert "crawler_resource_cpu_utilization 0.0000" in text


def test_cpu_gate_is_warm_only_after_sample(monkeypatch):
    # Unbounded (no quota/budget) -> always warm, so the full multiplier is safe.
    assert CpuGate().is_warm() is True

    # Bounded via cpu_budget but with no sample yet -> cold start.
    gate = CpuGate(cpu_budget=2.0)
    assert gate.is_warm() is False

    # Feed a real usage reading so the gate can take its first sample.
    monkeypatch.setattr(
        "crawlutron.runtime.pressure_gates.read_cpu_usage_seconds",
        lambda root="/sys/fs/cgroup": 1.0,
    )
    gate.cpu_ratio()  # first sample establishes the baseline
    assert gate.is_warm() is True


# --- sensing ---


def test_read_memory_stats_v2(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.max").write_text("3221225472\n")  # 3 GiB
    (cg / "memory.current").write_text("1610612736\n")  # 1.5 GiB
    stats = read_memory_stats(cgroup_root=str(cg))
    assert stats == MemoryStats(total_bytes=3221225472, used_bytes=1610612736)


def test_read_memory_stats_v2_max_string_falls_back_to_total_ram(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.current").write_text("104857600\n")
    stats = read_memory_stats(cgroup_root=str(cg), total_ram=8 * 1024**3)
    assert stats.total_bytes == 8 * 1024**3


def test_read_memory_stats_v1(tmp_path):
    cg = tmp_path / "cgroup" / "memory"
    cg.mkdir(parents=True)
    (cg / "memory.limit_in_bytes").write_text("2147483648\n")
    (cg / "memory.usage_in_bytes").write_text("536870912\n")
    stats = read_memory_stats(cgroup_root=str(tmp_path / "cgroup"))
    assert stats.total_bytes == 2147483648
    assert stats.used_bytes == 536870912


def test_read_memory_stats_missing_files_falls_back_to_total_ram(tmp_path):
    stats = read_memory_stats(cgroup_root=str(tmp_path / "does-not-exist"), total_ram=4 * 1024**3)
    assert stats.total_bytes == 4 * 1024**3
    # The fallback senses the process's OWN RSS (adversarial pass 5): a 0 usage
    # left the ratio at 0 and the gate open on the branch that claims to gate.
    assert 0 < stats.used_bytes < stats.total_bytes, stats
