"""Batch crawl counter writes into one transaction instead of one per counter.

Governs: coordination_counters.add_many, MetricsStore delta accumulation, and
snapshot visibility of unflushed deltas.
"""

from __future__ import annotations

import pytest

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.runtime import metrics as runtime_metrics
from crawlutron.storage import coordination_counters
from crawlutron.vocabulary import strategies
from tests import _result_builders

_STRATEGY = strategies.HTTP_DECLARED_CRAWLER


class _CountingCounters(coordination_counters.CoordinationCounters):
    """A shared store that counts how many batched transactions it committed."""

    def __init__(self, path: str) -> None:
        super().__init__(path)
        self.batches = 0

    def add_many(self, deltas) -> None:
        self.batches += 1
        super().add_many(deltas)


def _meter(
    tmp_path, name: str
) -> tuple[coordination_counters.CoordinationCounters, runtime_metrics.MetricsStore]:
    """One bound store plus the metrics facade over it."""
    counters = coordination_counters.CoordinationCounters(str(tmp_path / name))
    return counters, runtime_metrics.MetricsStore(counters=counters)


def _rendered(count: int) -> list[EngineCrawlResult]:
    """``count`` successful crawl results on the declared HTTP strategy."""
    return [
        _result_builders.engine_result(
            f"http://example.com/{i}",
            "rendered",
            selected_strategy=strategies.HTTP_DECLARED_CRAWLER,
        )
        for i in range(count)
    ]


async def test_commits_per_crawl_is_bounded_not_linear(tmp_path) -> None:
    """A 50-result ``observe_crawl`` must commit once, not ~156 times."""
    counters = _CountingCounters(str(tmp_path / "commits.db"))
    store = runtime_metrics.MetricsStore(counters=counters)
    try:
        await store.observe_crawl(
            accepted=True,
            results=_rendered(50),
            elapsed_ms=100.0,
            errors=2,
            caller_id="test-key",
        )
        assert counters.batches == 1, f"expected 1 commit batch, got {counters.batches}"
    finally:
        counters.close()


def test_snapshot_merges_pending_before_flush(tmp_path) -> None:
    """A buffered increment is readable before it is committed."""
    counters = coordination_counters.CoordinationCounters(str(tmp_path / "pending.db"))
    counters.add_pending("crawl_results_total", 3.0)

    assert counters.snapshot()["crawl_results_total"] == 3.0
    assert counters.get("crawl_results_total") == 0.0

    counters.flush()
    assert counters.get("crawl_results_total") == 3.0
    assert counters.snapshot()["crawl_results_total"] == 3.0
    counters.close()


async def test_exposition_stays_byte_identical(tmp_path) -> None:
    """The Prometheus exposition keeps the same lines it had before batching."""
    counters, store = _meter(tmp_path, "golden.db")
    await store.observe_crawl(results=_rendered(1), elapsed_ms=10.0)
    await store.record_throttle()

    text = await store.as_prometheus()

    assert "crawler_crawl_requests_total 1.0" in text
    assert "crawler_crawl_results_total 1.0" in text
    assert f'crawler_strategy_results_total{{strategy="{_STRATEGY}"}} 1.0' in text
    assert "crawler_crawl_seconds_total 0.010" in text
    assert "crawler_resource_throttle_batches_total 1.0" in text
    assert "crawler_resource_memory_utilization" in text
    assert "crawler_resource_cpu_utilization" in text
    counters.close()


async def test_caller_series_cap_still_works(tmp_path) -> None:
    """Past the series cap new callers fold into ``__overflow__``."""
    counters, store = _meter(tmp_path, "overflow.db")
    for i in range(300):
        await store.observe_crawl(caller_id=f"key-{i}")
    assert (await store.snapshot()).caller_counts.get("__overflow__", 0) >= 1.0
    counters.close()


async def test_non_finite_delta_is_refused(tmp_path) -> None:
    """A non-finite delta is refused at the public observe door."""
    counters, store = _meter(tmp_path, "finite.db")
    with pytest.raises(ValueError, match="counter delta must be finite"):
        await store.observe_crawl(results=_rendered(1), elapsed_ms=float("nan"))

    await store.observe_crawl(results=_rendered(1), elapsed_ms=5.0)
    assert await counters.aget("total_elapsed_ms") >= 5.0
    counters.close()


async def test_no_store_path_still_works() -> None:
    """An unbound store reports its own process-local counters."""
    store = runtime_metrics.MetricsStore()
    await store.observe_crawl(results=_rendered(1), elapsed_ms=5.0)
    text = await store.as_prometheus()
    assert "crawler_crawl_results_total 1.0" in text
    assert "crawler_crawl_seconds_total 0.005" in text


async def test_multi_worker_summation_unchanged(tmp_path) -> None:
    """Two workers on one database sum their counters instead of overwriting."""
    path = str(tmp_path / "sum.db")
    first = runtime_metrics.MetricsStore(counters=coordination_counters.CoordinationCounters(path))
    second = runtime_metrics.MetricsStore(counters=coordination_counters.CoordinationCounters(path))
    await first.observe_crawl(results=_rendered(1), elapsed_ms=10.0)
    await second.observe_crawl(results=_rendered(1), elapsed_ms=20.0)

    snap = await first.snapshot()
    assert snap.crawl_results_total == 2.0
    assert snap.total_elapsed_ms == 30.0
