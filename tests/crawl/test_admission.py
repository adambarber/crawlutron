"""Admission control: CPU and memory gates, weighted strategy admission and page-budget bounds."""

import asyncio

import pytest

from app.health.readiness import ReadinessState
from app.runtime.memory_pressure import MemoryGate, MemoryStats, read_memory_stats
from app.runtime.metrics import get_metrics
from app.runtime.resource_pressure import CpuGate, ResourceGate, read_cpu_quota, read_cpu_usage_seconds
from tests._timing_harnesses import recorder


class TestReadinessState:
    def test_initial_state_is_ready(self):
        state = ReadinessState()
        snap = state.snapshot()
        assert snap.status == "ready"
        assert snap.active_requests == 0
        assert snap.saturated is False

    def test_begin_end_request(self):
        state = ReadinessState()
        state.begin_request()
        snap = state.snapshot()
        assert snap.active_requests == 1
        assert snap.saturated is False
        state.end_request()
        snap = state.snapshot()
        assert snap.active_requests == 0

    # Each gate rides at its own target (memory 0.92, cpu 0.90); the values below are over-the-line readings,
    # and they are the only data the property differs on.
    @pytest.mark.parametrize(
        ("gauge", "over_the_line"),
        [("resource_memory_utilization", 0.95), ("resource_cpu_utilization", 0.93)],
    )
    def test_saturated_when_a_gate_is_over_its_line(self, gauge, over_the_line):
        """Both gates carry the same shape: ready below the line, saturated above it.

        One test over the two gauges because the shape is the property -- zero
        the other gauge so this one is what decides, read ready, push this one
        over, read saturated. Written per gate it was twice the same walk with a
        different number, and the CPU copy had quietly stopped asserting the
        status string the memory copy checked.
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


def test_read_cpu_quota_v2(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "cpu.max").write_text("200000 100000\n")  # 2 CPUs
    assert read_cpu_quota(cgroup_root=str(cg)) == 2.0


def test_read_cpu_quota_v2_max_is_unbounded(tmp_path):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "cpu.max").write_text("max 100000\n")
    assert read_cpu_quota(cgroup_root=str(cg)) is None


def test_read_cpu_quota_v1(tmp_path):
    cg = tmp_path / "cgroup" / "cpu"
    cg.mkdir(parents=True)
    (cg / "cpu.cfs_quota_us").write_text("150000\n")
    (cg / "cpu.cfs_period_us").write_text("100000\n")
    assert read_cpu_quota(cgroup_root=str(tmp_path / "cgroup")) == 1.5


def test_read_cpu_quota_v1_unlimited(tmp_path):
    cg = tmp_path / "cgroup" / "cpu"
    cg.mkdir(parents=True)
    (cg / "cpu.cfs_quota_us").write_text("-1\n")
    (cg / "cpu.cfs_period_us").write_text("100000\n")
    assert read_cpu_quota(cgroup_root=str(tmp_path / "cgroup")) is None


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

    Every CPU-throttle test needs the same three things -- a pinned quota, a
    clock it owns, and a way to say "the box used this much CPU over this much
    wall time" without waiting for either -- and each of them was spelling the
    same two one-element lists. The gate itself stays real: only the two sensors
    it reads are replaced, so the utilization arithmetic under test is
    production's.
    """
    clock, usage = [0.0], [0.0]
    monkeypatch.setattr("app.runtime.pressure_gates.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.runtime.pressure_gates.read_cpu_usage_seconds", lambda root="": usage[0])
    monkeypatch.setattr("app.runtime.pressure_gates.read_cpu_quota", lambda root="": 2.0)
    return CpuGate(target=target), clock, usage


def test_cpu_gate_full_speed_below_target(monkeypatch):
    gate, clock, usage = _cpu_gate(monkeypatch)
    assert gate.batch_scale_factor() == 1.0  # first sight -> 0 utilization
    # 1.0 CPU-second over 1.0 wall-second on a 2-CPU budget -> 0.50 util (< target)
    clock[0], usage[0] = 1.0, 1.0
    assert gate.batch_scale_factor() == 1.0


def test_cpu_gate_scales_between_target_and_hard(monkeypatch):
    gate, clock, usage = _cpu_gate(monkeypatch)
    gate.batch_scale_factor()  # initialize sample
    # 1.85 CPU-seconds over 1.0 wall-second on 2 CPUs -> 0.925 util (mid soft zone)
    clock[0], usage[0] = 1.0, 1.85
    s = gate.batch_scale_factor()
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


def test_cpu_gate_zero_at_hard_ceiling(monkeypatch):
    gate, clock, usage = _cpu_gate(monkeypatch)
    gate.batch_scale_factor()
    # 2.0 CPU-seconds over 1.0 wall-second on 2 CPUs -> 1.0 util (>= hard)
    clock[0], usage[0] = 1.0, 2.0
    assert gate.batch_scale_factor() == 0.0


# The two sub-gates a throttle can be asked about, named once.
GATE_KINDS = (
    ("cpu", lambda: CpuGate(target=0.90)),
    ("memory", lambda: MemoryGate(target=0.92)),
)


_GATE_KIND_IDS = [kind for kind, _ in GATE_KINDS]


async def test_cpu_gate_unbounded_is_noop(monkeypatch):
    """No cgroup quota and no manual budget -> never throttles."""
    gate = CpuGate(target=0.90)
    monkeypatch.setattr("app.runtime.pressure_gates.read_cpu_quota", lambda root="": None)
    monkeypatch.setattr("app.runtime.pressure_gates.read_cpu_usage_seconds", lambda root="": 999.0)
    assert gate.batch_scale_factor() == 1.0
    sleeps = recorder(monkeypatch)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert sleeps.delays == []


@pytest.mark.parametrize(("kind", "make_gate"), GATE_KINDS, ids=_GATE_KIND_IDS)
async def test_gate_blocks_above_hard_ceiling_until_usage_drains(monkeypatch, kind, make_gate):
    """Above the hard ceiling the gate makes callers wait; it releases once
    usage drains back under the target."""
    sleeps = recorder(monkeypatch)
    gate = make_gate()
    samples = iter([0.99, 0.98, 0.80])
    gate.usage_ratio = lambda: next(samples)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert len(sleeps.delays) >= 2, f"the {kind} gate returned before usage drained"
    assert sleeps.delays[-1] <= 1.0  # short polling intervals, never long ones


@pytest.mark.parametrize(("kind", "make_gate"), GATE_KINDS, ids=_GATE_KIND_IDS)
async def test_gate_returns_immediately_under_target(monkeypatch, kind, make_gate):
    gate = make_gate()
    gate.usage_ratio = lambda: 0.50
    sleeps = recorder(monkeypatch)
    await gate.wait_while_saturated(max_wait_seconds=10)  # must not block
    assert sleeps.delays == [], f"the {kind} gate blocked while it was under target"


# --- combined ResourceGate ---


def _pinned_resource_gate(monkeypatch, *, cpu_ratio, memory_ratio, max_wait_seconds=0.05):
    """A ResourceGate with both sampler ratios pinned, an injected clock, and recorded waits.

    The clock and the sleep are one seam, not two: the recorded sleep advances
    the clock by exactly the interval it was asked to wait, so the wait
    terminates on the gate's OWN ``max_wait_seconds`` instead of on a real one.
    Faking only the sleep is what made this fixture dishonest -- with the sleep
    free but the deadline on the real monotonic clock, the bound the helper
    advertises is a bound no test could afford to reach, so the callers had to
    pass a ten-second ``max_wait_seconds`` that overrode it and then sat out
    those ten real seconds. The wait is still a wait: the gate polls, yields,
    and re-reads both ratios, exactly as in production.
    """
    now = [0.0]
    gate = ResourceGate(
        clock=lambda: now[0],
        cpu_ratio=lambda: cpu_ratio,
        memory_ratio=lambda: memory_ratio,
    )
    gate.memory.max_wait_seconds = max_wait_seconds
    gate.cpu.max_wait_seconds = max_wait_seconds
    waited = []
    yield_to_loop = asyncio.sleep

    async def recording_sleep(seconds):
        waited.append(seconds)
        now[0] += seconds
        await yield_to_loop(0)

    monkeypatch.setattr(asyncio, "sleep", recording_sleep)
    return gate, waited


async def test_resource_gate_waits_while_either_saturated(monkeypatch):
    gate, waited = _pinned_resource_gate(monkeypatch, cpu_ratio=0.99, memory_ratio=0.0)
    # Memory idle, CPU saturated -> gate must still wait (either resource binds).
    await gate.wait_while_saturated()
    assert waited, "ResourceGate returned without waiting while CPU was saturated"
    assert sum(waited) >= gate.memory.max_wait_seconds, (
        f"the gate gave up after {sum(waited)}s of a {gate.memory.max_wait_seconds}s "
        "budget; the wait has to be bounded by the bound, not by returning early"
    )


async def test_resource_gate_returns_when_both_idle(monkeypatch):
    gate, waited = _pinned_resource_gate(monkeypatch, cpu_ratio=0.0, memory_ratio=0.0)
    await gate.wait_while_saturated()
    assert waited == []


async def test_resource_gate_explicit_budget_overrides_the_pinned_bound(monkeypatch):
    """The argument still wins over the gate's own bound, observably.

    The test above exercises the bound the helper pins (0.05s -> one poll at the
    0.1s poll interval). This pins the other half of the contract, so "no
    argument" cannot quietly become "the argument is ignored" and so the two
    bounds are distinguishable in the recorded poll count rather than only in
    the source: 0.25s of budget is three polls, 0.05s of budget is one.
    """
    gate, waited = _pinned_resource_gate(monkeypatch, cpu_ratio=0.99, memory_ratio=0.0)
    await gate.wait_while_saturated(max_wait_seconds=0.25)
    assert len(waited) == 3, f"a 0.25s budget produced {len(waited)} polls, not 3: {waited}"


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
        "app.runtime.pressure_gates.read_cpu_usage_seconds",
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


def test_read_memory_stats_v2_max_string_falls_back_to_total_ram(tmp_path, monkeypatch):
    cg = tmp_path / "cgroup"
    cg.mkdir()
    (cg / "memory.max").write_text("max\n")
    (cg / "memory.current").write_text("104857600\n")
    monkeypatch.setattr("app.runtime.memory_pressure._total_system_ram", lambda: 8 * 1024**3)
    stats = read_memory_stats(cgroup_root=str(cg))
    assert stats.total_bytes == 8 * 1024**3


def test_read_memory_stats_v1(tmp_path):
    cg = tmp_path / "cgroup" / "memory"
    cg.mkdir(parents=True)
    (cg / "memory.limit_in_bytes").write_text("2147483648\n")
    (cg / "memory.usage_in_bytes").write_text("536870912\n")
    stats = read_memory_stats(cgroup_root=str(tmp_path / "cgroup"))
    assert stats.total_bytes == 2147483648
    assert stats.used_bytes == 536870912


def test_read_memory_stats_missing_files_falls_back_to_total_ram(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runtime.memory_pressure._total_system_ram", lambda: 4 * 1024**3)
    stats = read_memory_stats(cgroup_root=str(tmp_path / "does-not-exist"))
    assert stats.total_bytes == 4 * 1024**3
    assert stats.used_bytes == 0
