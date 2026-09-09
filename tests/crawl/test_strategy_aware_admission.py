"""Tests for strategy-aware resource admission (item 7).

HTTP crawls use far less CPU/memory than browser crawls. When the CPU gate
reports ample headroom, the dispatcher should allow more HTTP-heavy batches
to run (riding the CPU-implied scale instead of being throttled by memory
held by browser processes), while browser-bound batches stay on the combined
memory+CPU gate.
"""

import asyncio

import pytest

from app.constants import (
    BROWSER_HEADLESS,
    HTTP_DECLARED_CRAWLER,
)
from app.crawl.recursive import _batch_is_http_heavy, run_recursive_crawl
from app.models import CrawlTask, EngineCrawlResult
from app.resource_pressure import ResourceGate


# ---------------------------------------------------------------------------
# Pure helper: does the upcoming batch look HTTP-heavy?
# ---------------------------------------------------------------------------
class _HSScheduler:
    """Fake scheduler exposing per-domain pinned strategies."""

    def __init__(self, pins=None):
        self.pins = dict(pins or {})
        self.runs = 0

    def get_pinned_strategy(self, domain):
        return self.pins.get(domain)

    async def run(self, jobs, worker):
        self.runs += 1
        return [await worker(j) for j in jobs]


def _task(domain):
    url = f"http://{domain}/"
    return CrawlTask(index=0, original_url=url, normalized_url=url, domain=domain)


class TestBatchIsHttpHeavy:
    def test_all_http_pinned(self):
        sched = _HSScheduler({f"site{i}.gov": HTTP_DECLARED_CRAWLER for i in range(4)})
        frontier = [(0, _task(f"site{i}.gov")) for i in range(4)]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is True

    def test_majority_http_pinned(self):
        # 2 of 3 pinned HTTP; the 3rd unpinned (not yet proven cheap)
        sched = _HSScheduler({"a.gov": HTTP_DECLARED_CRAWLER, "b.gov": HTTP_DECLARED_CRAWLER})
        frontier = [(0, _task(d)) for d in ["a.gov", "b.gov", "c.gov"]]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is True

    def test_half_or_less_not_http_heavy(self):
        # 1 of 3 pinned HTTP -> not HTTP-heavy (conservative)
        sched = _HSScheduler({"a.gov": HTTP_DECLARED_CRAWLER})
        frontier = [(0, _task(d)) for d in ["a.gov", "b.gov", "c.gov"]]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is False

    def test_browser_pinned_counts_against(self):
        # 2 browser-pinned, 1 HTTP-pinned -> not HTTP-heavy
        sched = _HSScheduler(
            {
                "a.gov": BROWSER_HEADLESS,
                "b.gov": BROWSER_HEADLESS,
                "c.gov": HTTP_DECLARED_CRAWLER,
            }
        )
        frontier = [(0, _task(d)) for d in ["a.gov", "b.gov", "c.gov"]]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is False

    def test_no_pins_is_false(self):
        sched = _HSScheduler({})  # cold start, nothing proven cheap
        frontier = [(0, _task(f"site{i}.gov")) for i in range(4)]
        assert _batch_is_http_heavy(sched, frontier, sample=8) is False

    def test_empty_frontier_is_false(self):
        sched = _HSScheduler({})
        assert _batch_is_http_heavy(sched, [], sample=8) is False

    def test_sample_window_limits_lookahead(self):
        # Beyond the sample window the composition is not considered.
        sched = _HSScheduler({"a.gov": HTTP_DECLARED_CRAWLER})
        # first task unpinned, second pinned HTTP; sample=1 looks only at first
        frontier = [(0, _task("z.gov")), (0, _task("a.gov"))]
        assert _batch_is_http_heavy(sched, frontier, sample=1) is False


# ---------------------------------------------------------------------------
# Dispatcher wiring: apply http_scale_factor for HTTP-heavy batches.
# ---------------------------------------------------------------------------
class _RecordingGate:
    """Gate fake that records which scale method the dispatcher consulted."""

    def __init__(self, batch=0.5, http=1.0):
        self.batch = batch
        self.http = http
        self.calls = []
        self.waits = 0

    async def wait_while_saturated(self):
        self.waits += 1

    def batch_scale_factor(self):
        self.calls.append("batch")
        return self.batch

    def http_scale_factor(self):
        self.calls.append("http")
        return self.http

    def memory_ratio(self):
        return 0.0

    def cpu_ratio(self):
        return 0.0


class _Spill:
    def __init__(self):
        self.writes = {}

    def write(self, index, result):
        self.writes[index] = result


def _mk_result(url):
    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[],
        http_status=200,
        headers={},
        content_type="text/html",
        html="",
        text="t",
        extraction_method="http_html",
        quality_score=1.0,
        discovered_links=[],
    )


async def _run_with_gate(sched, gate, jobs):
    async def worker(job):
        return _mk_result(job.normalized_url)

    async def robots_allows(url):
        return True

    spill = _Spill()
    await run_recursive_crawl(
        jobs=jobs,
        scheduler=sched,
        worker=worker,
        robots_allows=robots_allows,
        robots_disallowed=lambda job: _mk_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]),
        spill=spill,
        shutdown_event=asyncio.Event(),
        max_depth=2,
        max_pages=100,
        respect_robots=True,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=4,
        seed_sitemaps=False,
        resource_gate=gate,
    )
    return spill


class TestDispatcherStrategyAwareScaling:
    async def test_http_heavy_batch_uses_http_scale(self):
        # Two domains already proven HTTP (pinned) -> dispatcher consults http_scale_factor
        sched = _HSScheduler(
            {
                "a.gov": HTTP_DECLARED_CRAWLER,
                "b.gov": HTTP_DECLARED_CRAWLER,
            }
        )
        jobs = [_task("a.gov"), _task("b.gov")]
        gate = _RecordingGate(batch=0.0, http=1.0)
        await _run_with_gate(sched, gate, jobs)

        # The dispatcher consults http_scale_factor for an HTTP-heavy batch and
        # uses its value (http=1.0) so both pages fit in one batch.
        assert "http" in gate.calls
        assert sched.runs == 1  # http scale 1.0 => both admitted at once

    async def test_browser_bound_batch_uses_combined_scale(self):
        # Browser-pinned domains -> dispatcher stays on the combined gate
        sched = _HSScheduler(
            {
                "a.gov": BROWSER_HEADLESS,
                "b.gov": BROWSER_HEADLESS,
            }
        )
        jobs = [_task("a.gov"), _task("b.gov")]
        gate = _RecordingGate(batch=1.0, http=1.0)
        await _run_with_gate(sched, gate, jobs)

        assert "batch" in gate.calls
        assert "http" not in gate.calls

    async def test_cold_unpinned_batch_stays_on_combined_scale(self):
        # No pins yet (cold start) -> conservative combined gate (safety):
        # unpinned tasks may escalate to browser, so do not ignore memory.
        sched = _HSScheduler({})  # cold start
        jobs = [_task("a.gov"), _task("b.gov")]
        gate = _RecordingGate(batch=1.0, http=1.0)
        await _run_with_gate(sched, gate, jobs)

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
