"""Prometheus metrics; counters live in the shared store, gauges stay
process-local (measuring a cgroup every worker shares). The store is optional:
without one the counters are a plain process-local dict.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.runtime import process_state

if TYPE_CHECKING:
    from crawlutron.storage.coordination_counters import CoordinationCounters

# Labelled families: the store keys a family under "<prefix><label>", so one
# prefix read yields every series in it.
_STRATEGY_PREFIX = "strategy:"
_CLASSIFICATION_PREFIX = "classification:"
_PROVIDER_PREFIX = "search_provider:"
_CALLER_PREFIX = "caller:"
_SEARCH_CALLER_PREFIX = "search_caller:"

# Fail-closed bound on distinct caller-labelled series per family; a NEW caller
# past it folds into one reserved overflow series so bare and sum stay exact.
_MAX_CALLER_SERIES = 256
_OVERFLOW_SERIES = "__overflow__"


@dataclass(frozen=True)
class CountersSnapshot:
    """One read of every counter, as a typed record.

    This record replaces the store's accessor pile: a new counter is one field
    plus its write, not a property, an exposition line and a hope the spellings agree.
    """

    crawl_requests_total: float = 0.0
    crawl_requests_accepted_total: float = 0.0
    crawl_results_total: float = 0.0
    crawl_success_total: float = 0.0
    crawl_failure_total: float = 0.0
    crawl_errors_total: float = 0.0
    total_elapsed_ms: float = 0.0
    search_requests_total: float = 0.0
    search_results_total: float = 0.0
    search_errors_total: float = 0.0
    search_total_elapsed_ms: float = 0.0
    resource_throttle_batches_total: float = 0.0
    extraction_doc_pool_unavailable_total: float = 0.0
    strategy_counts: dict[str, float] = field(default_factory=dict)
    classification_counts: dict[str, float] = field(default_factory=dict)
    search_provider_counts: dict[str, float] = field(default_factory=dict)
    caller_counts: dict[str, float] = field(default_factory=dict)
    search_caller_counts: dict[str, float] = field(default_factory=dict)


class MetricsStore:
    """Service counters, optionally shared across worker processes."""

    def __init__(self, counters: CoordinationCounters | None = None) -> None:
        self._counters = counters
        # The no-store path. One dict for every counter and label, so a counter
        # is added in one place whether or not a store is attached.
        self._local: dict[str, float] = {}
        # One write lock: the local RMW and the caller-series check-then-insert
        # are read-modify-writes two threads can interleave.
        self._write_lock = threading.RLock()
        # Gauges: instantaneous readings of shared state, never summed.
        self.resource_memory_utilization: float = 0.0
        self.resource_cpu_utilization: float = 0.0

    # Writes happen only in observe/record; every read is one source dict
    # turned into one CountersSnapshot.

    def _add(self, key: str, delta: float) -> None:
        if self._counters is not None:
            self._counters.add(key, delta)
            return
        with self._write_lock:
            self._local[key] = self._local.get(key, 0.0) + delta

    def _source(self) -> dict[str, float]:
        return self._counters.snapshot() if self._counters is not None else self._local

    def _labelled(self, source: dict[str, float], prefix: str) -> dict[str, float]:
        return {key[len(prefix) :]: value for key, value in source.items() if key.startswith(prefix)}

    def _caller_series(self, source: dict[str, float], prefix: str, caller_id: str) -> str:
        """The series a caller id is written under, bounded by an overflow fold.

        A NEW caller past the cap folds into one reserved overflow series so the
        label dimension stays bounded while bare and sum(labels) stay exact.
        """
        key = prefix + caller_id
        if key in source:
            return caller_id
        distinct = sum(1 for existing in source if existing.startswith(prefix))
        return _OVERFLOW_SERIES if distinct >= _MAX_CALLER_SERIES else caller_id

    def snapshot(self) -> CountersSnapshot:
        """Every counter as one typed record, from one read of the source."""
        source = self._source()
        return CountersSnapshot(
            crawl_requests_total=source.get("crawl_requests_total", 0.0),
            crawl_requests_accepted_total=source.get("crawl_requests_accepted_total", 0.0),
            crawl_results_total=source.get("crawl_results_total", 0.0),
            crawl_success_total=source.get("crawl_success_total", 0.0),
            crawl_failure_total=source.get("crawl_failure_total", 0.0),
            crawl_errors_total=source.get("crawl_errors_total", 0.0),
            total_elapsed_ms=source.get("total_elapsed_ms", 0.0),
            search_requests_total=source.get("search_requests_total", 0.0),
            search_results_total=source.get("search_results_total", 0.0),
            search_errors_total=source.get("search_errors_total", 0.0),
            search_total_elapsed_ms=source.get("search_total_elapsed_ms", 0.0),
            resource_throttle_batches_total=source.get("resource_throttle_batches_total", 0.0),
            extraction_doc_pool_unavailable_total=source.get("extraction_doc_pool_unavailable_total", 0.0),
            strategy_counts=self._labelled(source, _STRATEGY_PREFIX),
            classification_counts=self._labelled(source, _CLASSIFICATION_PREFIX),
            search_provider_counts=self._labelled(source, _PROVIDER_PREFIX),
            caller_counts=self._labelled(source, _CALLER_PREFIX),
            search_caller_counts=self._labelled(source, _SEARCH_CALLER_PREFIX),
        )

    # -- the only counter write paths --------------------------------------

    def observe_crawl(
        self,
        *,
        accepted: bool = False,
        results: Sequence[EngineCrawlResult] | None = None,
        elapsed_ms: float = 0.0,
        errors: int = 0,
        caller_id: str = "",
    ) -> None:
        self._add("crawl_requests_total", 1)
        if caller_id:
            # Decision and insert under one lock: a check-then-insert split
            # across them races past the series cap.
            with self._write_lock:
                series = self._caller_series(self._source(), _CALLER_PREFIX, caller_id)
                self._add(_CALLER_PREFIX + series, 1)
        if accepted:
            self._add("crawl_requests_accepted_total", 1)
        if results:
            self._add("crawl_results_total", len(results))
            for result in results:
                if result.status == "success":
                    self._add("crawl_success_total", 1)
                else:
                    self._add("crawl_failure_total", 1)
                classification = result.classification
                if classification:
                    self._add(_CLASSIFICATION_PREFIX + str(classification), 1)
                strategy = result.selected_strategy
                if strategy:
                    self._add(_STRATEGY_PREFIX + str(strategy), 1)
        self._add("total_elapsed_ms", elapsed_ms)
        self._add("crawl_errors_total", errors)

    def observe_search(
        self,
        *,
        provider: str,
        results: int = 0,
        elapsed_ms: float = 0.0,
        errors: int = 0,
        caller_id: str = "",
    ) -> None:
        self._add("search_requests_total", 1)
        self._add("search_results_total", results)
        self._add("search_errors_total", errors)
        if provider:
            self._add(_PROVIDER_PREFIX + provider, results)
        if caller_id:
            with self._write_lock:
                series = self._caller_series(self._source(), _SEARCH_CALLER_PREFIX, caller_id)
                self._add(_SEARCH_CALLER_PREFIX + series, 1)
        self._add("search_total_elapsed_ms", elapsed_ms)

    def record_throttle(self) -> None:
        """Count one batch shrunk by the admission gate; a method rather than
        ``+= 1`` on a property, because a setter cannot know its own delta without
        a read-then-write that loses increments under concurrent setters.
        """
        self._add("resource_throttle_batches_total", 1)

    def record_doc_pool_unavailable(self) -> None:
        """Count one document acquisition that degraded: no worker within the
        budget (a wedged or retired slot), or a spawn that never became ready.
        The doc-pool wedge alert -- a warning log alone is invisible.
        """
        self._add("extraction_doc_pool_unavailable_total", 1)

    @property
    def counters(self) -> CoordinationCounters | None:
        """The shared storage backing these counters, or None before the
        lifespan binds it. A reader in another worker only sees these totals
        once this is a store rather than this process's own dict."""
        return self._counters

    def bind_counters(self, counters: CoordinationCounters) -> None:
        """Attach shared storage mid-life, carrying over any local counters --
        carried over unconditionally, since a non-empty store can be a sibling's
        and is not evidence this process's counts reached it.
        """
        if self._counters is not None:
            self._counters = counters
            return
        carried = self._local
        self._counters = counters
        for key, value in carried.items():
            counters.add(key, value)
        self._local = {}

    def reset(self) -> None:
        """Zero every counter and gauge in place, including the shared store; a
        reset that cleared only process memory would leave the endpoint's next read
        unchanged, but an operator expects agreement after an incident.
        """
        if self._counters is not None:
            self._counters.reset()
        else:
            self._local.clear()
        self.resource_memory_utilization = 0.0
        self.resource_cpu_utilization = 0.0

    def as_prometheus(self, *, caller_detail: bool = True) -> str:
        """Render the exposition; ``caller_detail`` gates the per-caller families.

        The router passes the operator-auth result so the caller-labelled series
        (a key-identity oracle) only leave the door to an authenticated operator.
        """
        return _exposition(self.snapshot(), self, caller_detail=caller_detail)


@dataclass(frozen=True)
class _Family:
    """One declared exposition family: its name, help, kind, label and
    source are stated once here; a new counter is one table row plus its write.
    """

    name: str
    help_text: str = ""
    kind: str = "counter"  # "counter" or "gauge"
    label: str | None = None  # label key; None for a bare series
    field: str = ""  # attribute on the snapshot (or the store, when store=True)
    store: bool = False  # gauges read the process-local store, never the record
    elapsed_ms: bool = False  # a millisecond field rendered as seconds
    caller_detail: bool = False  # gated behind the operator token
    header: bool = True  # False: legacy labelled families emitted bare (no HELP/TYPE)
    fmt: str = ""  # explicit float format; bare counters render str(value)


_FAMILIES: tuple[_Family, ...] = (
    _Family("crawler_crawl_requests_total", "Total crawl requests received.", field="crawl_requests_total"),
    _Family(
        "crawler_crawl_requests_accepted_total",
        "Total crawl requests accepted via webhook callback (HTTP 202).",
        field="crawl_requests_accepted_total",
    ),
    _Family("crawler_crawl_results_total", "Total crawl results produced.", field="crawl_results_total"),
    _Family("crawler_crawl_success_total", "Total successful crawl results.", field="crawl_success_total"),
    _Family("crawler_crawl_failure_total", "Total failed crawl results.", field="crawl_failure_total"),
    _Family(
        "crawler_crawl_errors_total", "Total rejected/invalid crawl requests.", field="crawl_errors_total"
    ),
    _Family(
        "crawler_search_requests_total", "Total web search requests received.", field="search_requests_total"
    ),
    _Family(
        "crawler_search_results_total", "Total web search results returned.", field="search_results_total"
    ),
    _Family("crawler_search_errors_total", "Total failed web search requests.", field="search_errors_total"),
    _Family(
        "crawler_extraction_doc_pool_unavailable_total",
        "Document-extraction acquisitions degraded: no pool worker was available within the budget.",
        field="extraction_doc_pool_unavailable_total",
    ),
    _Family(
        "crawler_search_results_by_provider_total",
        "Total web search results returned, per provider.",
        label="provider",
        field="search_provider_counts",
    ),
    _Family(
        "crawler_crawl_requests_by_caller_total",
        "Crawl requests received, per caller id.",
        label="caller",
        field="caller_counts",
        caller_detail=True,
    ),
    _Family(
        "crawler_search_requests_by_caller_total",
        "Search requests received, per caller id.",
        label="caller",
        field="search_caller_counts",
        caller_detail=True,
    ),
    _Family(
        "crawler_search_seconds_total",
        "Total wall-clock time spent on web searches (seconds).",
        field="search_total_elapsed_ms",
        elapsed_ms=True,
    ),
    _Family(
        "crawler_crawl_seconds_total",
        "Total wall-clock time spent crawling (seconds).",
        field="total_elapsed_ms",
        elapsed_ms=True,
    ),
    _Family(
        "crawler_strategy_results_total",
        label="strategy",
        field="strategy_counts",
        header=False,
    ),
    _Family(
        "crawler_result_classification_total",
        label="classification",
        field="classification_counts",
        header=False,
    ),
    _Family(
        "crawler_resource_memory_utilization",
        "Current memory usage as a fraction of the cgroup memory limit (or host RAM)."
        " >1 means over the hard ceiling.",
        kind="gauge",
        field="resource_memory_utilization",
        store=True,
        fmt=".4f",
    ),
    _Family(
        "crawler_resource_cpu_utilization",
        "Current CPU utilization as a fraction of the cgroup CPU quota (or manual budget)."
        " >1 means over budget / being throttled.",
        kind="gauge",
        field="resource_cpu_utilization",
        store=True,
        fmt=".4f",
    ),
    _Family(
        "crawler_resource_throttle_batches_total",
        "Crawl batches shrunk by resource admission control (memory and/or CPU pressure).",
        field="resource_throttle_batches_total",
    ),
)

# The bare counter families, in exposition order -- the declaration the golden
# test reads; public on purpose so the suite pins it without a private reach.
COUNTER_FAMILIES = tuple(f for f in _FAMILIES if f.kind == "counter" and f.label is None and f.header)


def _family_value(family: _Family, record: CountersSnapshot, store: MetricsStore) -> str:
    """One family's rendered value."""
    source: MetricsStore | CountersSnapshot = store if family.store else record
    value = getattr(source, family.field)
    if family.elapsed_ms:
        return f"{value / 1000.0:.3f}"
    if family.fmt:
        return f"{value:{family.fmt}}"
    return f"{value}"


def _family_lines(family: _Family, record: CountersSnapshot, store: MetricsStore) -> list[str]:
    """One family's exposition block: HELP, TYPE, then its series."""
    lines: list[str] = []
    if family.header:
        lines = [f"# HELP {family.name} {family.help_text}", f"# TYPE {family.name} {family.kind}"]
    if family.label is None:
        lines.append(f"{family.name} {_family_value(family, record, store)}")
    else:
        counts: dict[str, float] = getattr(record, family.field)
        lines.extend(
            f'{family.name}{{{family.label}="{key}"}} {value}' for key, value in sorted(counts.items())
        )
    return lines


def _exposition(record: CountersSnapshot, store: MetricsStore, *, caller_detail: bool) -> str:
    """Render the Prometheus text exposition for one counters record.

    Pure formatting over the declared family table: one backing-store
    read per scrape, and the line contract lives in _FAMILIES, not here.
    """
    lines: list[str] = []
    for family in _FAMILIES:
        if family.caller_detail and not caller_detail:
            continue
        lines.extend(_family_lines(family, record, store))
    return "\n".join(lines) + "\n"


def _gauge_and_throttle_lines(record: CountersSnapshot, store: MetricsStore) -> list[str]:
    """The gauges (process-local, so still off the store) and throttle counter."""
    lines = [
        "# HELP crawler_resource_memory_utilization Current memory usage as a fraction of the "
        "cgroup memory limit (or host RAM). >1 means over the hard ceiling.",
        "# TYPE crawler_resource_memory_utilization gauge",
        f"crawler_resource_memory_utilization {store.resource_memory_utilization:.4f}",
        "# HELP crawler_resource_cpu_utilization Current CPU utilization as a fraction of the "
        "cgroup CPU quota (or manual budget). >1 means over budget / being throttled.",
        "# TYPE crawler_resource_cpu_utilization gauge",
        f"crawler_resource_cpu_utilization {store.resource_cpu_utilization:.4f}",
        "# HELP crawler_resource_throttle_batches_total Crawl batches shrunk by resource admission "
        "control (memory and/or CPU pressure).",
        "# TYPE crawler_resource_throttle_batches_total counter",
        f"crawler_resource_throttle_batches_total {record.resource_throttle_batches_total}",
    ]
    return lines


metrics = MetricsStore()


def _reset() -> None:
    """Empty the process-local metrics store (registry route, conftest)."""
    metrics.reset()


def _has() -> bool:
    """Whether the metrics store holds any recorded counter right now."""
    return bool(metrics._source())


process_state.register(__name__, _reset, _has)


def get_metrics() -> MetricsStore:
    """Return the metrics singleton.

    Callers use this accessor rather than importing ``metrics`` so they observe
    the current store even if a test or harness swaps the singleton.
    """
    return metrics
