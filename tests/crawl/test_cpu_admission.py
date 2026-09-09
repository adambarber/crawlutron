"""CPU-adaptive admission control (#self-tuning).

Extends the self-tuning admission control from memory-only to CPU. The service
senses its cgroup CPU quota and current utilization (a rate, not a level) and
throttles crawl-batch dispatch so aggregate CPU rides at the configured target
instead of exceeding the quota and getting throttled by the kernel. The
ResourceGate combines memory + CPU so the most-contended resource sets the pace.
"""

from app.resource_pressure import (
    CpuGate,
    ResourceGate,
    read_cpu_quota,
    read_cpu_usage_seconds,
)

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


def _fake_clock_and_usage(monkeypatch, clock, usage):
    monkeypatch.setattr("app.resource_pressure.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.resource_pressure.read_cpu_usage_seconds", lambda root="": usage[0])
    monkeypatch.setattr("app.resource_pressure.read_cpu_quota", lambda root="": 2.0)


def test_cpu_gate_full_speed_below_target(monkeypatch):
    gate = CpuGate(target=0.90)
    clock, usage = [0.0], [0.0]
    _fake_clock_and_usage(monkeypatch, clock, usage)
    assert gate.batch_scale_factor() == 1.0  # first sight -> 0 utilization
    # 1.0 CPU-second over 1.0 wall-second on a 2-CPU budget -> 0.50 util (< target)
    clock[0], usage[0] = 1.0, 1.0
    assert gate.batch_scale_factor() == 1.0


def test_cpu_gate_scales_between_target_and_hard(monkeypatch):
    gate = CpuGate(target=0.90)
    clock, usage = [0.0], [0.0]
    _fake_clock_and_usage(monkeypatch, clock, usage)
    gate.batch_scale_factor()  # initialize sample
    # 1.85 CPU-seconds over 1.0 wall-second on 2 CPUs -> 0.925 util (mid soft zone)
    clock[0], usage[0] = 1.0, 1.85
    s = gate.batch_scale_factor()
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


def test_cpu_gate_zero_at_hard_ceiling(monkeypatch):
    gate = CpuGate(target=0.90)
    clock, usage = [0.0], [0.0]
    _fake_clock_and_usage(monkeypatch, clock, usage)
    gate.batch_scale_factor()
    # 2.0 CPU-seconds over 1.0 wall-second on 2 CPUs -> 1.0 util (>= hard)
    clock[0], usage[0] = 1.0, 2.0
    assert gate.batch_scale_factor() == 0.0


async def test_cpu_gate_unbounded_is_noop(monkeypatch):
    """No cgroup quota and no manual budget -> never throttles."""
    gate = CpuGate(target=0.90)
    monkeypatch.setattr("app.resource_pressure.read_cpu_quota", lambda root="": None)
    monkeypatch.setattr("app.resource_pressure.read_cpu_usage_seconds", lambda root="": 999.0)
    assert gate.batch_scale_factor() == 1.0
    waited = []

    async def fake_sleep(s):
        waited.append(s)

    monkeypatch.setattr("app.resource_pressure.asyncio.sleep", fake_sleep)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert waited == []


async def test_cpu_gate_blocks_above_hard_ceiling(monkeypatch):
    gate = CpuGate(target=0.90)
    samples = iter([0.99, 0.98, 0.80])
    waits = []

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr(gate, "_usage_ratio", lambda: next(samples))
    monkeypatch.setattr("app.resource_pressure.asyncio.sleep", fake_sleep)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert len(waits) >= 2
    assert waits[-1] <= 1.0


async def test_cpu_gate_returns_immediately_under_target(monkeypatch):
    gate = CpuGate(target=0.90)
    monkeypatch.setattr(gate, "_usage_ratio", lambda: 0.50)
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("app.resource_pressure.asyncio.sleep", fake_sleep)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert waited == []


# --- combined ResourceGate ---


async def test_resource_gate_waits_while_either_saturated(monkeypatch):
    gate = ResourceGate()
    gate.memory.max_wait_seconds = 0.05
    gate.cpu.max_wait_seconds = 0.05
    # Memory idle, CPU saturated -> gate must still wait (either resource binds).
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.99)
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.0)
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("app.resource_pressure.asyncio.sleep", fake_sleep)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert waited, "ResourceGate returned without waiting while CPU was saturated"


async def test_resource_gate_returns_when_both_idle(monkeypatch):
    gate = ResourceGate()
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.0)
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.0)
    waited = []

    async def fake_sleep(seconds):
        waited.append(seconds)

    monkeypatch.setattr("app.resource_pressure.asyncio.sleep", fake_sleep)
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert waited == []


def test_resource_gate_scale_is_min_of_resources(monkeypatch):
    gate = ResourceGate()
    # Memory at 99% -> scale 0; CPU idle -> scale 1. Combined must be 0.
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.99)
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.0)
    assert gate.batch_scale_factor() == 0.0
    # Flip: CPU saturated, memory idle -> still 0.
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.0)
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.99)
    assert gate.batch_scale_factor() == 0.0
    # Both idle -> full speed.
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.0)
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.0)
    assert gate.batch_scale_factor() == 1.0


def test_metrics_expose_resource_utilization_and_counters():
    from app.metrics import get_metrics

    text = get_metrics().as_prometheus()
    assert "crawler_resource_memory_utilization" in text
    assert "crawler_resource_cpu_utilization" in text
    assert "crawler_resource_throttle_batches_total" in text
    assert "crawler_resource_pause_seconds_total" in text
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
        "app.resource_pressure.read_cpu_usage_seconds",
        lambda root="/sys/fs/cgroup": 1.0,
    )
    gate.cpu_ratio()  # first sample establishes the baseline
    assert gate.is_warm() is True
