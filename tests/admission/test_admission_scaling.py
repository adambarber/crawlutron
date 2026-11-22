"""Batches scale with pressure; HTTP-heavy work escapes memory accounting."""

import asyncio
import itertools
import time

import pytest

from crawlutron.runtime.metrics import get_metrics
from crawlutron.runtime.pressure_sizing import ResourceGate
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from crawlutron_admit.sensing.pressure_memory import batch_scale
from tests._admission_doubles import FakeResourceGauge
from tests._fetcher_factories import LINKED_PAIR_LIMITS, crawled_results, site_linking_to
from tests._recursive_harnesses import drive_recursive_crawl, make_crawl_task
from tests._scheduler_doubles import FakeScheduler as _Scheduler

# --- governance ---


@pytest.mark.parametrize(
    ("used", "expected"),
    [(0.80, 1.0), (0.99, 0.0)],
    ids=["below-target-runs-full", "at-the-hard-ceiling-stops"],
)
def test_batch_scale_at_the_utilization_edges(used, expected):
    assert batch_scale(used=used, total=1.0, target=0.92) == expected


def test_batch_scale_scales_linearly_between_target_and_hard():
    # Halfway into the soft zone -> roughly half throughput
    s = batch_scale(used=0.945, total=1.0, target=0.92)
    assert 0.0 < s < 1.0
    assert abs(s - 0.5) < 0.2


async def _crawl_under_pressure(*, cpu_ratio, memory_ratio):
    """Run the two-page fixture crawl against a gate with pinned sampler ratios.
    Returns the ``(envelope, throttle_count)`` pair; the pinned resource drives
    the throttle, and the gate is handed to the engine as its collaborator."""
    from crawlutron_admit.sensing.pressure_gates import ResourceGate

    gate = ResourceGate(memory_ratio=lambda: memory_ratio, cpu_ratio=lambda: cpu_ratio)
    before = (await get_metrics().snapshot()).resource_throttle_batches_total
    pages = site_linking_to("/a")
    out = await crawled_results(pages, limits=LINKED_PAIR_LIMITS, resource_gate=gate)
    throttled = (await get_metrics().snapshot()).resource_throttle_batches_total - before
    return out, throttled


@pytest.mark.parametrize(
    ("cpu_ratio", "memory_ratio", "throttled"),
    [(0.0, 0.89, False), (0.93, 0.0, True)],
    ids=["http-escapes-memory", "cpu-pressure-throttles"],
)
async def test_engine_admission_under_pressure(cpu_ratio, memory_ratio, throttled):
    """HTTP work rides a free CPU (item 7); CPU pressure still throttles."""
    out, observed = await _crawl_under_pressure(cpu_ratio=cpu_ratio, memory_ratio=memory_ratio)
    assert {r.normalized_url.raw for r in out["results"]} == {
        "http://example.com/",
        "http://example.com/a",
    }
    assert (observed > 0) is throttled


def test_direct_reads_do_not_perturb_slope(monkeypatch):
    """The usage sensor is read by readiness/metrics/waits between dispatch
    scale computations. Those reads must not mutate the slope series, or a
    single stray sample invents a climb that is not there."""
    import crawlutron_admit.sensing.pressure_memory as mp

    # The clock and the sensor keep going: the event loop and the framework's
    # teardown read the global clock too, and a finite sequence ran out.
    ratios = itertools.chain([0.5, 0.5, 0.1, 0.5, 0.5], itertools.repeat(0.5))
    ticks = itertools.count(0.0, 10.0)
    sampled: list[float] = []

    def sampling_clock() -> float:
        value = next(ticks)
        sampled.append(value)
        return value

    monkeypatch.setattr(
        mp,
        "read_memory_stats",
        lambda cgroup_root="/sys/fs/cgroup": mp.MemoryStats(
            total_bytes=1000, used_bytes=int(next(ratios) * 1000)
        ),
    )
    monkeypatch.setattr(time, "monotonic", sampling_clock)
    gate = mp.MemoryGate(target=0.9, hard=1.0)
    assert gate.leading_usage_ratio() == 0.5
    assert gate.leading_usage_ratio() == 0.5
    gate.usage_ratio()  # stray readiness/metrics read of a dip sample
    assert gate.leading_usage_ratio() == 0.5
    # The series holds one sample per scale computation, and the direct read
    # took none: the leading ratio cannot show it, so the series is read here.
    assert len(sampled) == 3, (
        f"the slope series holds {len(sampled)} samples for three scale computations: a direct read took one"
    )


async def _ok_worker(job):
    from tests._result_builders import engine_result

    return engine_result(job.normalized_url, "rendered")


async def _robots_allow_all(url):
    return True


async def _run_with_gate(sched, gate, jobs):
    """Drive the recursive crawl with the pinned gate as its only resource authority.

    The spill, worker, robots gate, shutdown event and every keyword belong to
    the harness; a test states the jobs, pins and scales only.
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
    async def test_http_heavy_batch_uses_gate_ratios(self):
        # Two HTTP-pinned domains: the one gate still decides from both resource ratios.
        sched = _Scheduler(
            {
                "a.gov": HTTP_DECLARED_CRAWLER,
                "b.gov": HTTP_DECLARED_CRAWLER,
            }
        )
        jobs = [make_crawl_task(0, "a.gov"), make_crawl_task(0, "b.gov")]
        gate = FakeResourceGauge(batch=0.0, http=1.0)
        await _run_with_gate(sched, gate, jobs)

        # The kernel gate consults both ratios and admits the whole batch at capacity.
        assert "mem" in gate.calls and "cpu" in gate.calls
        assert sched.runs == 1

    @pytest.mark.parametrize(
        ("_case", "pins"),
        [
            # Browser-pinned domains -> dispatcher stays on the combined gate.
            (
                "browser_bound",
                {"a.gov": BROWSER_HEADLESS, "b.gov": BROWSER_HEADLESS},
            ),
            # No pins yet (cold start) -> conservative combined gate (safety): unpinned tasks may escalate to
            # browser, so do not ignore memory.
            ("cold_unpinned", {}),
        ],
    )
    async def test_batch_uses_gate_ratios(self, _case, pins):
        jobs = [make_crawl_task(0, "a.gov"), make_crawl_task(0, "b.gov")]
        gate = FakeResourceGauge(batch=1.0, http=1.0)
        await _run_with_gate(_Scheduler(pins), gate, jobs)

        assert "mem" in gate.calls and "cpu" in gate.calls
        assert "batch" not in gate.calls and "http" not in gate.calls


# --------------------------------------------------------------------------- Gate semantics: HTTP scale
# escapes the browser-held-memory bottleneck.
class TestHttpScaleEscapesMemory:
    def test_http_scale_ignores_memory_when_cpu_headroom(self, monkeypatch):
        """CpuGate.headroom_fraction()."""
        # CPU has ample headroom (0.20 -> headroom 1.0 > 0.5).
        gate = ResourceGate(
            target_mem=0.85,
            target_cpu=0.90,
            cpu_budget=4.0,
            memory_ratio=lambda: 0.89,
            cpu_ratio=lambda: 0.20,
        )

        http_scale = gate.http_scale_factor()
        memory_scale = gate.memory.batch_scale_factor()

        # HTTP work is NOT memory-bound: it rides the CPU scale, escaping the browser-held-memory bottleneck
        # that throttles browser-bound batches.
        assert http_scale > memory_scale
        assert memory_scale < 1.0  # memory gate would clamp browser work
        assert http_scale == pytest.approx(1.0, rel=0.01)

    def test_http_scale_respects_tight_cpu(self, monkeypatch):
        """When CPU is itself tight, http_scale falls back to the combined gate."""
        gate = ResourceGate(
            target_mem=0.85,
            target_cpu=0.90,
            cpu_budget=4.0,
            memory_ratio=lambda: 0.60,
            cpu_ratio=lambda: 0.93,  # headroom 0.4 < 0.5
        )

        http_scale = gate.http_scale_factor()
        # Combined batch_scale = min(cpu 0.4, mem 1.0) = 0.4
        assert http_scale == pytest.approx(0.4, rel=0.01)

    def test_http_scale_and_browser_scale_diverge(self, monkeypatch):
        """Same resources: HTTP scale (CPU-driven) exceeds the combined/browser scale."""
        gate = ResourceGate(
            target_mem=0.85,
            target_cpu=0.90,
            cpu_budget=4.0,
            memory_ratio=lambda: 0.89,  # falling off
            cpu_ratio=lambda: 0.30,  # headroom > 0.5
        )

        http_scale = gate.http_scale_factor()
        browser_scale = gate.batch_scale_factor()
        assert http_scale > browser_scale
        assert browser_scale < 1.0
        assert http_scale == pytest.approx(1.0, rel=0.01)


async def test_production_http_lane_rides_cpu_under_browser_memory_pressure() -> None:
    """Item 7 is wired at the seam: GENERAL rides CPU, HEAVY stays min-all."""
    from crawlutron.runtime.pressure_sizing import AdmissionPolicy, TaskRamBudget, kernel_lane_specs
    from crawlutron_admit import Count, LaneId, MinAll, RideCpuWhenFree, Want
    from tests._support._gate_factory import GateSpec, build_gate

    policy = AdmissionPolicy(
        http_task_bytes=48 * 1024 * 1024,
        browser_task_bytes=700 * 1024 * 1024,
        browser_pool_reserve_bytes=0,
        max_http_concurrency=32,
        max_browser_concurrency=8,
    )
    source = kernel_lane_specs(policy, TaskRamBudget())[LaneId.GENERAL].scale_source
    assert isinstance(source, RideCpuWhenFree)

    async def general_grant(scale_source):
        gate, _ = build_gate(
            GateSpec(
                memory_usage=0.88,
                mem_target=0.85,
                mem_hard=0.90,
                cpu_usage=0.10,
                cpu_target=0.90,
                cpu_hard=0.95,
                scale_source=scale_source,
                ramp_floor=128,
                ramp_max=128,
                initial_ramp=128,
                memory_budget_mib=100_000,
            )
        )
        want = Want.of(general=Count(16), heavy=Count(1))
        async with gate.admit(want) as granted:
            return granted.allocation.for_lane(LaneId.GENERAL).value

    riding = await asyncio.wait_for(general_grant(source), 1.0)
    min_all = await asyncio.wait_for(general_grant(MinAll()), 1.0)
    assert riding > min_all
