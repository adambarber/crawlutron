"""Batches scale with pressure; HTTP-heavy work escapes memory accounting."""

import itertools
import time

import pytest

from crawlutron.orchestration.recursive import batch_is_http_heavy
from crawlutron.runtime.pressure_memory import batch_scale
from crawlutron.runtime.pressure_sizing import ResourceGate
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS, HTTP_DECLARED_CRAWLER
from tests._admission_doubles import PinnedGate
from tests._admission_doubles import pinned_resource_gate as _pinned_resource_gate
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


async def _crawl_under_pressure(monkeypatch, *, cpu_ratio, memory_ratio):
    """Run the two-page fixture crawl against a gate with pinned sampler ratios.

    Returns the ``(envelope, waits)`` pair; only the pinned resource drives the
    throttle, and the engine is handed the pinned gate as its collaborator.
    """
    gate, waits = _pinned_resource_gate(monkeypatch, cpu_ratio=cpu_ratio, memory_ratio=memory_ratio)

    pages = site_linking_to("/a")
    return await crawled_results(pages, limits=LINKED_PAIR_LIMITS, resource_gate=gate), waits


@pytest.mark.parametrize(
    ("cpu_ratio", "memory_ratio", "saturated"),
    [(0.0, 0.99, "memory"), (0.99, 0.0, "CPU")],
    ids=["memory", "cpu"],
)
async def test_engine_throttles_batches_under_pressure(monkeypatch, cpu_ratio, memory_ratio, saturated):
    """Integration: with one sampler pinned at 99%, the engine pauses dispatch
    (gate waits) yet the crawl still completes, the other resource idle."""
    out, waits = await _crawl_under_pressure(monkeypatch, cpu_ratio=cpu_ratio, memory_ratio=memory_ratio)
    # Engine results carry the value object; identity is its ``.raw`` text.
    assert {r.normalized_url.raw for r in out["results"]} == {
        "http://example.com/",
        "http://example.com/a",
    }
    assert waits, f"engine dispatched without consulting the resource gate under {saturated} pressure"


def test_direct_reads_do_not_perturb_slope(monkeypatch):
    """The usage sensor is read by readiness/metrics/waits between dispatch
    scale computations. Those reads must not mutate the slope series, or a
    single stray sample invents a climb that is not there."""
    import crawlutron.runtime.pressure_memory as mp

    # The clock and the sensor keep going: the event loop and the framework's
    # teardown read the global clock too, and a finite sequence ran out.
    ratios = itertools.chain([0.5, 0.5, 0.1, 0.5, 0.5], itertools.repeat(0.5))
    ticks = itertools.count(0.0, 10.0)
    sampled: list[float] = []

    def _clock() -> float:
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
    monkeypatch.setattr(time, "monotonic", _clock)
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


# --------------------------------------------------------------------------- Pure helper: does the upcoming
# batch look HTTP-heavy? ---------------------------------------------------------------------------


def _http_heavy(pins: dict[str, str], domains: list[str], *, sample: int = 8) -> bool:
    """Build the batch the way the admission gate sees it and ask if it is HTTP-heavy.

    The scheduler double, frontier construction and sample size used to be
    re-spelled at every site; a test now states its pins and verdict only.
    """
    frontier = [(0, make_crawl_task(0, d)) for d in domains]
    return batch_is_http_heavy(_Scheduler(pins), frontier, sample=sample)


class TestBatchIsHttpHeavy:
    def test_all_http_pinned(self):
        sched = _Scheduler({f"site{i}.gov": HTTP_DECLARED_CRAWLER for i in range(4)})
        frontier = [(0, make_crawl_task(0, f"site{i}.gov")) for i in range(4)]
        assert batch_is_http_heavy(sched, frontier, sample=8) is True

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
        # Cold start, nothing proven cheap
        assert _http_heavy({}, [f"site{i}.gov" for i in range(4)]) is False

    def test_empty_frontier_is_false(self):
        assert _http_heavy({}, []) is False

    def test_sample_window_limits_lookahead(self):
        # Beyond the sample window the composition is not considered.
        sched = _Scheduler({"a.gov": HTTP_DECLARED_CRAWLER})
        # First task unpinned, second pinned HTTP; sample=1 looks only at first
        frontier = [(0, make_crawl_task(0, "z.gov")), (0, make_crawl_task(0, "a.gov"))]
        assert batch_is_http_heavy(sched, frontier, sample=1) is False


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

        # The dispatcher consults http_scale_factor for an HTTP-heavy batch and uses its value (http=1.0) so
        # both pages fit in one batch.
        assert "http" in gate.calls
        assert sched.runs == 1  # http scale 1.0 => both admitted at once

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
    async def test_batch_uses_the_combined_scale(self, _case, pins):
        jobs = [make_crawl_task(0, "a.gov"), make_crawl_task(0, "b.gov")]
        gate = PinnedGate(batch=1.0, http=1.0)
        await _run_with_gate(_Scheduler(pins), gate, jobs)

        assert "batch" in gate.calls
        assert "http" not in gate.calls


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
