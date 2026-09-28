"""Admission control: CPU and memory gates, weighted strategy admission and page-budget bounds."""

import asyncio
import time

import pytest

from app.constants import (
    BROWSER_HEADLESS,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.payload_helpers import _payload_policy
from app.crawl.policy import PolicyTier
from app.crawl.recursive_admission import _batch_is_http_heavy
from app.health.readiness import ReadinessState
from app.memory_pressure import (
    MemoryGate,
    MemoryStats,
    batch_scale,
    read_memory_stats,
)
from app.metrics import get_metrics
from app.resource_pressure import (
    _BROWSER_RAM_FRACTION,
    _BROWSER_TASK_RAM_BYTES,
    _DOC_TASK_RAM_BYTES,
    _HTTP_TASK_RAM_BYTES,
    CpuGate,
    ResourceGate,
    TaskRamBudget,
    WeightedAdmission,
    _build_admission_policy,
    default_admission_policy,
    read_cpu_quota,
    read_cpu_usage_seconds,
)
from tests._fetcher_factories import LINKED_PAIR_LIMITS, crawled_results, site_linking_to
from tests._policy_factories import admit
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task
from tests._scheduler_doubles import FakeScheduler as _Scheduler
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

    # Each gate rides at its own target (memory 0.92, cpu 0.90); the values below
    # are over-the-line readings, and they are the only data the property differs on.
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
    monkeypatch.setattr("app.resource_pressure.time.monotonic", lambda: clock[0])
    monkeypatch.setattr("app.resource_pressure.read_cpu_usage_seconds", lambda root="": usage[0])
    monkeypatch.setattr("app.resource_pressure.read_cpu_quota", lambda root="": 2.0)
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


# The two sub-gates a throttle can be asked about, named once. The wait
# behaviour under test is the same for both, so every throttle test runs against
# both -- which is only a guarantee while the pair lives in one declaration: two
# tests each spelling their own list is how one of them ends up covering CPU
# twice and memory not at all.
GATE_KINDS = (
    ("cpu", lambda: CpuGate(target=0.90)),
    ("memory", lambda: MemoryGate(target=0.92)),
)
_GATE_KIND_IDS = [kind for kind, _ in GATE_KINDS]


async def test_cpu_gate_unbounded_is_noop(monkeypatch):
    """No cgroup quota and no manual budget -> never throttles."""
    gate = CpuGate(target=0.90)
    monkeypatch.setattr("app.resource_pressure.read_cpu_quota", lambda root="": None)
    monkeypatch.setattr("app.resource_pressure.read_cpu_usage_seconds", lambda root="": 999.0)
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
    monkeypatch.setattr(gate, "_usage_ratio", lambda: next(samples))
    await gate.wait_while_saturated(max_wait_seconds=10)
    assert len(sleeps.delays) >= 2, f"the {kind} gate returned before usage drained"
    assert sleeps.delays[-1] <= 1.0  # short polling intervals, never long ones


@pytest.mark.parametrize(("kind", "make_gate"), GATE_KINDS, ids=_GATE_KIND_IDS)
async def test_gate_returns_immediately_under_target(monkeypatch, kind, make_gate):
    gate = make_gate()
    monkeypatch.setattr(gate, "_usage_ratio", lambda: 0.50)
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
    gate = ResourceGate(clock=lambda: now[0])
    gate.memory.max_wait_seconds = max_wait_seconds
    gate.cpu.max_wait_seconds = max_wait_seconds
    monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: cpu_ratio)
    monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: memory_ratio)
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
    # No max_wait_seconds argument, on purpose: the bound under test is the one
    # the helper pins, not one the test supplies to make the wait affordable.
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
    monkeypatch.setattr("app.memory_pressure._total_system_ram", lambda: 8 * 1024**3)
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
    monkeypatch.setattr("app.memory_pressure._total_system_ram", lambda: 4 * 1024**3)
    stats = read_memory_stats(cgroup_root=str(tmp_path / "does-not-exist"))
    assert stats.total_bytes == 4 * 1024**3
    assert stats.used_bytes == 0


# --- governance ---


def test_batch_scale_full_below_target():
    assert batch_scale(used=0.80, total=1.0, target=0.92) == 1.0


def test_batch_scale_scales_linearly_between_target_and_hard():
    # halfway into the soft zone -> roughly half throughput
    s = batch_scale(used=0.945, total=1.0, target=0.92)
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


def test_batch_scale_zero_at_hard_ceiling():
    assert batch_scale(used=0.99, total=1.0, target=0.92) == 0.0


async def _crawl_under_pressure(monkeypatch, *, cpu_ratio, memory_ratio):
    """Run the two-page fixture crawl against a gate with pinned sampler ratios.

    Returns the (envelope, waits) pair: the combined ResourceGate is
    exercised with only the pinned resource driving the throttle, and the
    engine is handed the pinned gate through the module attribute it reads.
    """
    import app.crawl.engine as engine_mod

    gate, waits = _pinned_resource_gate(monkeypatch, cpu_ratio=cpu_ratio, memory_ratio=memory_ratio)
    monkeypatch.setattr(engine_mod, "_resource_gate", gate)

    pages = site_linking_to("/a")
    return await crawled_results(pages, limits=LINKED_PAIR_LIMITS), waits


@pytest.mark.parametrize(
    ("cpu_ratio", "memory_ratio", "saturated"),
    [(0.0, 0.99, "memory"), (0.99, 0.0, "CPU")],
    ids=["memory", "cpu"],
)
async def test_engine_throttles_batches_under_pressure(monkeypatch, cpu_ratio, memory_ratio, saturated):
    """Integration: with one sampler pinned at 99%, the engine pauses dispatch
    (gate waits) yet the crawl still completes. The combined ResourceGate is
    exercised; the other resource is pinned idle so only the named one drives
    the throttle."""
    out, waits = await _crawl_under_pressure(monkeypatch, cpu_ratio=cpu_ratio, memory_ratio=memory_ratio)
    assert {r["normalized_url"] for r in out["results"]} == {
        "http://example.com",
        "http://example.com/a",
    }
    assert waits, f"engine dispatched without consulting the resource gate under {saturated} pressure"


def test_direct_reads_do_not_perturb_slope(monkeypatch):
    """L7: _usage_ratio() is read by readiness/metrics/waits between dispatch
    scale computations. Those reads must not mutate the slope series, or a
    single stray sample invents a climb that is not there."""
    import app.memory_pressure as mp

    ratios = iter([0.5, 0.5, 0.1, 0.5, 0.5])
    ticks = iter([0.0, 10.0, 20.0, 30.0, 40.0])
    monkeypatch.setattr(
        mp,
        "read_memory_stats",
        lambda cgroup_root="/sys/fs/cgroup": mp.MemoryStats(
            total_bytes=1000, used_bytes=int(next(ratios) * 1000)
        ),
    )
    monkeypatch.setattr(time, "monotonic", lambda: next(ticks))
    gate = mp.MemoryGate(target=0.9, hard=1.0)
    assert gate.leading_usage_ratio() == 0.5
    assert gate.leading_usage_ratio() == 0.5
    gate._usage_ratio()  # stray readiness/metrics read of a dip sample
    assert gate.leading_usage_ratio() == 0.5


# ---------------------------------------------------------------------------
# Pure helper: does the upcoming batch look HTTP-heavy?
# ---------------------------------------------------------------------------


def _http_heavy(pins: dict[str, str], domains: list[str], *, sample: int = 8) -> bool:
    """Build the batch the way the admission gate sees it and ask if it is HTTP-heavy.

    The scheduler double, the frontier construction and the sample size were
    spelled at every call site. The two tests that differ only in the pin map
    were the pair a reader could not tell apart: what each one asserts is which
    domains are pinned and whether that is enough. Now a test states its pins
    and its verdict and nothing else.
    """
    frontier = [(0, make_crawl_task(0, d)) for d in domains]
    return _batch_is_http_heavy(_Scheduler(pins), frontier, sample=sample)


class TestBatchIsHttpHeavy:
    def test_all_http_pinned(self):
        sched = _Scheduler({f"site{i}.gov": HTTP_DECLARED_CRAWLER for i in range(4)})
        frontier = [(0, make_crawl_task(0, f"site{i}.gov")) for i in range(4)]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is True

    def test_majority_http_pinned(self):
        # 2 of 3 pinned HTTP; the 3rd unpinned (not yet proven cheap)
        pins = {"a.gov": HTTP_DECLARED_CRAWLER, "b.gov": HTTP_DECLARED_CRAWLER}
        assert _http_heavy(pins, ["a.gov", "b.gov", "c.gov"]) is True

    def test_half_or_less_not_http_heavy(self):
        # 1 of 3 pinned HTTP -> not HTTP-heavy (conservative)
        assert _http_heavy({"a.gov": HTTP_DECLARED_CRAWLER}, ["a.gov", "b.gov", "c.gov"]) is False

    def test_browser_pinned_counts_against(self):
        # 2 browser-pinned, 1 HTTP-pinned -> not HTTP-heavy
        pins = {
            "a.gov": BROWSER_HEADLESS,
            "b.gov": BROWSER_HEADLESS,
            "c.gov": HTTP_DECLARED_CRAWLER,
        }
        assert _http_heavy(pins, ["a.gov", "b.gov", "c.gov"]) is False

    def test_no_pins_is_false(self):
        # cold start, nothing proven cheap
        assert _http_heavy({}, [f"site{i}.gov" for i in range(4)]) is False

    def test_empty_frontier_is_false(self):
        assert _http_heavy({}, []) is False

    def test_sample_window_limits_lookahead(self):
        # Beyond the sample window the composition is not considered.
        sched = _Scheduler({"a.gov": HTTP_DECLARED_CRAWLER})
        # first task unpinned, second pinned HTTP; sample=1 looks only at first
        frontier = [(0, make_crawl_task(0, "z.gov")), (0, make_crawl_task(0, "a.gov"))]
        assert _batch_is_http_heavy(sched, frontier, sample=1) is False


# ---------------------------------------------------------------------------
# Dispatcher wiring: apply http_scale_factor for HTTP-heavy batches.
# ---------------------------------------------------------------------------
class PinnedGate:
    """A ResourceGate with fixed scales and fixed ratios, recording consultations.

    What the two admission questions have in common: WHICH scale the dispatcher
    read (``calls``), and how saturated the box is (``mem``/``cpu``). One double
    answers both, so a test pins the scales without the ratios and vice versa,
    and a gate method a test starts needing is one method here rather than a
    second double.
    """

    def __init__(self, *, batch=1.0, http=1.0, mem=0.0, cpu=0.0):
        self.batch = batch
        self.http = http
        self._mem = mem
        self._cpu = cpu
        self.calls: list[str] = []

    async def wait_while_saturated(self):
        self.calls.append("wait")

    def batch_scale_factor(self):
        self.calls.append("batch")
        return self.batch

    def http_scale_factor(self):
        self.calls.append("http")
        return self.http

    def memory_ratio(self):
        return self._mem

    def cpu_ratio(self):
        return self._cpu


async def _ok_worker(job):
    from tests._result_builders import make_engine_result

    return make_engine_result(job.normalized_url)


async def _robots_allow_all(url):
    return True


async def _run_with_gate(sched, gate, jobs):
    """Drive the recursive crawl with the pinned gate as its only resource authority.

    The spill, the worker, the robots gate, the shutdown event and every
    ``run_recursive_crawl`` keyword belong to the harness; a test states the
    jobs, the scheduler's pins and the gate's scales.
    """
    await drive_recursive_crawl(
        jobs,
        _ok_worker,
        allows=_robots_allow_all,
        scheduler=sched,
        gate=gate,
        max_depth=2,
        max_pages=100,
    )


class TestDispatcherStrategyAwareScaling:
    async def test_http_heavy_batch_uses_http_scale(self):
        # Two domains already proven HTTP (pinned) -> dispatcher consults http_scale_factor
        sched = _Scheduler(
            {
                "a.gov": HTTP_DECLARED_CRAWLER,
                "b.gov": HTTP_DECLARED_CRAWLER,
            }
        )
        jobs = [make_crawl_task(0, "a.gov"), make_crawl_task(0, "b.gov")]
        gate = PinnedGate(batch=0.0, http=1.0)
        await _run_with_gate(sched, gate, jobs)

        # The dispatcher consults http_scale_factor for an HTTP-heavy batch and
        # uses its value (http=1.0) so both pages fit in one batch.
        assert "http" in gate.calls
        assert sched.runs == 1  # http scale 1.0 => both admitted at once

    @pytest.mark.parametrize(
        ("case", "pins"),
        [
            # Browser-pinned domains -> dispatcher stays on the combined gate.
            (
                "browser_bound",
                {"a.gov": BROWSER_HEADLESS, "b.gov": BROWSER_HEADLESS},
            ),
            # No pins yet (cold start) -> conservative combined gate (safety):
            # unpinned tasks may escalate to browser, so do not ignore memory.
            ("cold_unpinned", {}),
        ],
    )
    async def test_batch_uses_the_combined_scale(self, case, pins):
        jobs = [make_crawl_task(0, "a.gov"), make_crawl_task(0, "b.gov")]
        gate = PinnedGate(batch=1.0, http=1.0)
        await _run_with_gate(_Scheduler(pins), gate, jobs)

        assert "batch" in gate.calls
        assert "http" not in gate.calls


# ---------------------------------------------------------------------------
# Gate semantics: HTTP scale escapes the browser-held-memory bottleneck.
# ---------------------------------------------------------------------------
class TestHttpScaleEscapesMemory:
    def test_http_scale_ignores_memory_when_cpu_headroom(self, monkeypatch):
        """CpuGate.headroom_fraction()."""
        gate = ResourceGate(target_mem=0.85, target_cpu=0.90, cpu_budget=4.0)
        # Memory near the hard ceiling (0.89, held by browser processes); its
        # scale would be ~0.43 -- it would throttle cheap HTTP work.
        monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.89)
        # CPU has ample headroom (0.20 -> headroom 1.0 > 0.5).
        monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.20)

        http_scale = gate.http_scale_factor()
        memory_scale = gate.memory.batch_scale_factor()

        # HTTP work is NOT memory-bound: it rides the CPU scale, escaping the
        # browser-held-memory bottleneck that throttles browser-bound batches.
        assert http_scale > memory_scale
        assert memory_scale < 1.0  # memory gate would clamp browser work
        assert http_scale == pytest.approx(1.0, rel=0.01)

    def test_http_scale_respects_tight_cpu(self, monkeypatch):
        """When CPU is itself tight, http_scale falls back to the combined gate."""
        gate = ResourceGate(target_mem=0.85, target_cpu=0.90, cpu_budget=4.0)
        monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.60)
        monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.93)  # headroom 0.4 < 0.5

        http_scale = gate.http_scale_factor()
        # combined batch_scale = min(cpu 0.4, mem 1.0) = 0.4
        assert http_scale == pytest.approx(0.4, rel=0.01)

    def test_http_scale_and_browser_scale_diverge(self, monkeypatch):
        """Same resources: HTTP scale (CPU-driven) exceeds the combined/browser scale."""
        gate = ResourceGate(target_mem=0.85, target_cpu=0.90, cpu_budget=4.0)
        monkeypatch.setattr(gate.memory, "_usage_ratio", lambda: 0.89)  # falling off
        monkeypatch.setattr(gate.cpu, "_usage_ratio", lambda: 0.30)  # headroom > 0.5

        http_scale = gate.http_scale_factor()
        browser_scale = gate.batch_scale_factor()
        assert http_scale > browser_scale
        assert browser_scale < 1.0
        assert http_scale == pytest.approx(1.0, rel=0.01)


async def test_max_pages_default_is_bounded_not_unlimited():
    policy = _payload_policy({"url": "http://example.com/", "policy": {}}, PolicyTier.model_defaults())
    # DEFAULT_MAX_PAGES, not 0/"unlimited" (F4).
    assert policy.limits.max_pages == 100


async def test_max_pages_zero_falls_back_to_default():
    policy = _payload_policy(
        {"url": "http://example.com/", "policy": {"limits": {"max_pages": 0}}}, PolicyTier.model_defaults()
    )
    assert policy.limits.max_pages == 100


async def test_max_pages_above_upper_bound_is_rejected_not_clamped():
    # The model declares le=2000. This used to clamp, which meant the wire value
    # and the effective behaviour described different contracts; a rejection says
    # the same thing in both places.
    with pytest.raises(ValueError, match="invalid_policy_limits"):
        _payload_policy(
            {"url": "http://example.com/", "policy": {"limits": {"max_pages": 99999}}},
            PolicyTier.model_defaults(),
        )


async def test_max_pages_positive_preserved():
    policy = _payload_policy(
        {"url": "http://example.com/", "policy": {"limits": {"max_pages": 7}}}, PolicyTier.model_defaults()
    )
    assert policy.limits.max_pages == 7


async def test_max_pages_zero_bounded_at_engine():
    # Wide fixture: root links to 150 leaf pages. With max_pages=0 the crawl
    # must be bounded (DEFAULT_MAX_PAGES=100), not crawl all 151 reachable pages.
    pages = {
        "/": (200, "<html>" + "".join(f'<a href="/c{i}">c</a>' for i in range(150)) + "</html>", "text/html"),
    }
    for i in range(150):
        pages[f"/c{i}"] = (200, f"<html>child {i}</html>", "text/html")
    results = (await crawled_results(pages, limits={"max_depth": 1, "max_pages": 0}))["results"]
    assert 1 < len(results) <= 100
    # Without the bound it would have crawled root + 150 children = 151.
    assert len(results) < 151


def _policy(limit=3 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3, doc_workers=2):
    return _build_admission_policy(
        limit,
        pool_size=pool_size,
        max_concurrent_pages=max_concurrent_pages,
        # The real budget, not a test-local restatement of its fields: these
        # four call sites assert the policy that production's dataclass builds,
        # so a test that spelled the weights itself could pass against a
        # dataclass that no longer reserved them.
        budget=TaskRamBudget(doc_workers=doc_workers),
    )


def _unreserved_bytes(policy) -> int:
    """The 3 GiB limit minus the reserves that are already spent.

    Both reserves are RAM a consumer holds for the life of the process rather
    than RAM a request borrows, so the HTTP ceiling is sized from what is left.
    One function because two tests assert the same subtraction and a third
    that got it wrong would be invisible: the arithmetic IS the contract.
    """
    return (3 * 1024 * 1024 * 1024) - policy.browser_pool_reserve_bytes - policy.doc_pool_reserve_bytes


class TestPolicyDerivation:
    def test_browser_pool_reserve_subtracted_from_limit(self):
        # 3 GiB limit, 2 browsers * 700 MiB reserve and the default doc pool,
        # -> HTTP ceiling from the remainder.
        p = _policy()
        assert p.browser_pool_reserve_bytes == 2 * _BROWSER_TASK_RAM_BYTES
        assert p.doc_pool_reserve_bytes == 2 * _DOC_TASK_RAM_BYTES
        assert p.max_http_concurrency == max(1, _unreserved_bytes(p) // _HTTP_TASK_RAM_BYTES)
        # browser tier is capped at pool_size * max_concurrent_pages
        assert p.max_browser_concurrency == 2 * 3

    def test_http_ceiling_capped_at_hard_max(self):
        huge = _build_admission_policy(1024 * 1024 * 1024 * 1024, pool_size=2, max_concurrent_pages=3)
        assert huge.max_http_concurrency <= 512

    def test_smaller_limit_yields_smaller_http_ceiling(self):
        small = _policy(limit=1024 * 1024 * 1024)  # 1 GiB
        big = _policy(limit=3 * 1024 * 1024 * 1024)
        assert small.max_http_concurrency < big.max_http_concurrency

    def test_doc_pool_reserve_subtracted_from_limit(self):
        # The persistent markitdown workers are a fixed RAM consumer exactly
        # like the browser pool: each imports markitdown + the PDF/Office
        # parsers once and stays alive. Admission that ignores them sizes HTTP
        # concurrency from a budget that is already spent, which is the OOM
        # path under a hard 3 GiB ceiling.
        p = _policy(doc_workers=2)
        assert p.doc_pool_reserve_bytes == 2 * _DOC_TASK_RAM_BYTES
        assert p.max_http_concurrency == max(1, _unreserved_bytes(p) // _HTTP_TASK_RAM_BYTES)

    def test_doc_pool_reserve_lowers_the_http_ceiling(self):
        # The reserve must actually bite: admitting with doc workers must
        # admit strictly fewer concurrent HTTP tasks than without them.
        without = _policy(doc_workers=0)
        with_docs = _policy(doc_workers=2)
        assert with_docs.max_http_concurrency < without.max_http_concurrency

    def test_default_policy_reserves_the_default_doc_pool(self):
        # default_admission_policy is the conservative fallback (3 GiB box);
        # it must reserve the default doc worker count, not zero.
        gate = ResourceGate()
        wa = default_admission_policy(gate)
        assert wa._p.doc_pool_reserve_bytes == 2 * _DOC_TASK_RAM_BYTES


class TestWeightedAdmission:
    def test_headroom_from_most_contended_resource(self):
        gate = PinnedGate(mem=0.3, cpu=0.8)
        a = WeightedAdmission(gate, _policy())
        # min(1-0.3, 1-0.8) = 0.2
        assert a.headroom_fraction() == pytest.approx(0.2)

    def test_http_capacity_scales_with_headroom(self):
        gate = PinnedGate(mem=0.0, cpu=0.0)
        a = WeightedAdmission(gate, _policy())
        assert a.http_capacity() == a._p.max_http_concurrency
        gate2 = PinnedGate(mem=0.5, cpu=0.5)
        a2 = WeightedAdmission(gate2, _policy())
        assert a2.http_capacity() == pytest.approx(a._p.max_http_concurrency * 0.5, rel=0.01)

    async def test_admit_grants_all_when_headroom_full(self):
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=0.0), _policy())
        # Request exactly the tier ceiling so the claim ("grants everything
        # asked while headroom is full") tracks the sizing default, whatever it is.
        h, b = await a.admit(want_http=a._p.max_http_concurrency, want_browser=2)
        assert h == a._p.max_http_concurrency
        assert b == 2

    async def test_admit_caps_browser_tier(self):
        # Want more browsers than the tier allows; HTTP still granted.
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=0.0), _policy())
        h, b = await a.admit(want_http=10, want_browser=99)
        assert b == a.browser_capacity()  # capped
        assert h == 10

    async def test_admit_shrinks_http_under_memory_pressure(self):
        a = WeightedAdmission(PinnedGate(mem=0.9, cpu=0.0), _policy())
        h, b = await a.admit(want_http=200, want_browser=0)
        # headroom 0.1 -> http capacity ~10% of ceiling
        assert h < 200
        assert h > 0

    async def test_admit_yields_briefly_when_fully_saturated(self):
        # Saturated (headroom 0) with demand -> returns (0,0) after a short yield
        # instead of parking the whole crawl indefinitely.
        a = WeightedAdmission(PinnedGate(mem=1.0, cpu=1.0), _policy())
        t0 = asyncio.get_event_loop().time()
        h, b = await a.admit(want_http=5, want_browser=1)
        elapsed = asyncio.get_event_loop().time() - t0
        assert (h, b) == (0, 0)
        assert 0 < elapsed < 1.0  # yielded, did not block forever


class TestBrowserTierDoesNotDoubleScale:
    """T9: the browser pool's RAM is reserved up front, so the tier must not
    shrink against its own reserved footprint — only usage beyond the fraction
    the pool sizing already assumed should yield browser capacity."""

    def test_full_capacity_while_within_the_reserved_fraction(self):
        # Browsers using exactly the fraction derive_browser_pool_size assumed
        # is the steady state, not pressure.
        a = WeightedAdmission(PinnedGate(mem=_BROWSER_RAM_FRACTION, cpu=0.0), _policy())
        assert a.browser_capacity() == a._p.max_browser_concurrency

    def test_decays_only_for_usage_beyond_the_reserved_fraction(self):
        a = WeightedAdmission(PinnedGate(mem=_BROWSER_RAM_FRACTION + 0.2, cpu=0.0), _policy())
        # 20 points of surplus usage out of a 50-point ramp to exhaustion -> 60%
        # of the tier still admits.
        assert a.browser_capacity() == pytest.approx(a._p.max_browser_concurrency * 0.6, abs=1)

    def test_never_admits_when_saturated(self):
        a = WeightedAdmission(PinnedGate(mem=1.0, cpu=0.0), _policy())
        assert a.browser_capacity() == 0

    def test_cpu_saturation_still_shrinks_the_browser_tier(self):
        a = WeightedAdmission(PinnedGate(mem=0.0, cpu=1.0), _policy())
        assert a.browser_capacity() == 0


class TestHTTPTaskRamEstimate:
    """T10: the per-HTTP-task estimate must cover a real task — a max-length
    body plus the trafilatura/lxml parse state — or admission over-grants and
    the box swaps. 12 MiB measured optimistic at the review's live scale."""

    def test_default_covers_a_max_body_plus_parse_state(self):
        # A 10 MiB body plus lxml/trafilatura working set is 40+ MiB in practice.
        assert _HTTP_TASK_RAM_BYTES >= 48 * 1024 * 1024

    def test_http_ceiling_uses_the_same_bytes_as_admission(self):
        # Zero drift: the pool ceiling and the admission policy divide by the
        # same per-task estimate, whatever the default is.
        p = _policy()
        free = (3 * 1024 * 1024 * 1024) - p.browser_pool_reserve_bytes - p.doc_pool_reserve_bytes
        assert p.max_http_concurrency == max(1, free // _HTTP_TASK_RAM_BYTES)

    def test_the_3gib_default_still_admits_a_working_floor(self):
        # The minimal-deployment default (3 GiB, 2 browsers) must still admit
        # enough concurrent HTTP fetches for hundreds of pages per minute.
        p = _policy()
        assert p.max_http_concurrency >= 16


async def _no_crawl_delay(url: str) -> float | None:
    """A host that states no Crawl-delay, so a scenario about capacity can pass one."""
    return None


async def test_discovered_link_delay_commits_in_order_and_breaks_at_cap():
    events: list[tuple] = []
    links = [
        "https://example.test/first",
        "https://example.test/over-cap",
        "https://example.test/not-processed",
    ]

    async def allows(url: str) -> bool:
        events.append(("robots", url))
        return True

    async def crawl_delay(url: str) -> float:
        events.append(("delay", url))
        return 1.5

    def commit(domain: str, delay: float) -> None:
        events.append(("commit", domain, delay))

    frontier: list[str] = []
    outcome = await admit(
        links,
        frontier=frontier,
        max_pages=1,
        robots_allows=allows,
        robots_crawl_delay=crawl_delay,
        commit_crawl_delay=commit,
    )

    assert outcome.accepted == 1
    assert outcome.cap_blocked == 1
    assert frontier == ["https://example.test/first"]
    assert [event for event in events if event[0] == "delay"] == [
        ("delay", "https://example.test/first"),
        ("delay", "https://example.test/over-cap"),
    ]
    assert [event for event in events if event[0] == "commit"] == [
        ("commit", "example.test", 1.5),
        ("commit", "example.test", 1.5),
    ]
    assert not any(event == ("robots", "https://example.test/not-processed") for event in events)


async def test_duplicate_canonical_spellings_do_not_consume_capacity():
    frontier: list[str] = []
    outcome = await admit(
        [
            "https://example.test/child?utm_source=one",
            "https://example.test/child/",
            "https://example.test/other",
            "https://example.test/third",
        ],
        frontier=frontier,
        max_pages=2,
        respect_robots=False,
    )

    assert outcome.accepted == 2
    assert frontier == ["https://example.test/child", "https://example.test/other"]
    assert outcome.cap_blocked == 1


async def test_admission_robots_work_stops_at_remaining_capacity():
    calls: list[str] = []

    async def allows(url: str) -> bool:
        calls.append(url)
        return True

    frontier: list[str] = []
    outcome = await admit(
        [f"https://example.test/page-{index}" for index in range(100)],
        frontier=frontier,
        max_pages=1,
        known={"https://example.test/root"},
        robots_allows=allows,
        robots_crawl_delay=_no_crawl_delay,
        occupied=1,
    )

    assert calls == ["https://example.test/page-0"]
    assert outcome.accepted == 0
    assert outcome.cap_blocked == 1
