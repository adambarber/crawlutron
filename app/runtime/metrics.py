"""Prometheus metrics; counters live in the shared store, gauges stay
process-local (measuring a cgroup every worker shares). The store is optional:
without one the counters are a plain process-local dict.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.models.crawl_result import EngineCrawlResult

if TYPE_CHECKING:
    from app.storage.coordination_counters import CoordinationCounters

# Store keys. The Prometheus names differ, so the mapping is written out
# once in `as_prometheus` rather than derived from prefix/suffix surgery.
_CRAWL_REQUESTS = "crawl_requests_total"
_CRAWL_ACCEPTED = "crawl_requests_accepted_total"
_CRAWL_RESULTS = "crawl_results_total"
_CRAWL_SUCCESS = "crawl_success_total"
_CRAWL_FAILURE = "crawl_failure_total"
_CRAWL_ERRORS = "crawl_errors_total"
_CRAWL_ELAPSED = "total_elapsed_ms"
_SEARCH_REQUESTS = "search_requests_total"
_SEARCH_RESULTS = "search_results_total"
_SEARCH_ERRORS = "search_errors_total"
_SEARCH_ELAPSED = "search_total_elapsed_ms"
_THROTTLE_BATCHES = "resource_throttle_batches_total"

_STRATEGY_PREFIX = "strategy:"
_CLASSIFICATION_PREFIX = "classification:"
_PROVIDER_PREFIX = "search_provider:"
_CALLER_PREFIX = "caller:"
_SEARCH_CALLER_PREFIX = "search_caller:"

# Fail-closed bound on distinct caller-labelled series per family; a NEW caller
# past it folds into one reserved overflow series so bare and sum stay exact.
_MAX_CALLER_SERIES = 256
_OVERFLOW_SERIES = "__overflow__"

# Counter name -> the observable field callers read, built once so a
# counter cannot be recorded under one name and read under another.
_COUNTERS = {
    _CRAWL_REQUESTS: "crawl_requests_total",
    _CRAWL_ACCEPTED: "crawl_requests_accepted_total",
    _CRAWL_RESULTS: "crawl_results_total",
    _CRAWL_SUCCESS: "crawl_success_total",
    _CRAWL_FAILURE: "crawl_failure_total",
    _CRAWL_ERRORS: "crawl_errors_total",
    _CRAWL_ELAPSED: "total_elapsed_ms",
    _SEARCH_REQUESTS: "search_requests_total",
    _SEARCH_RESULTS: "search_results_total",
    _SEARCH_ERRORS: "search_errors_total",
    _SEARCH_ELAPSED: "search_total_elapsed_ms",
    _THROTTLE_BATCHES: "resource_throttle_batches_total",
}

# Declared for the checker and for readers: the counter fields are
# properties over `_COUNTERS`, so a rename cannot orphan a writer.
_COUNTER_FIELDS = frozenset(_COUNTERS.values())


class MetricsStore:
    """Service counters, optionally shared across worker processes."""

    def __init__(self, counters: CoordinationCounters | None = None) -> None:
        self._counters = counters
        # The no-store path. One dict for every counter and label, so a counter
        # is added in one place whether or not a store is attached.
        self._local: dict[str, float] = {}
        # Gauges: instantaneous readings of shared state, never summed.
        self.resource_memory_utilization: float = 0.0
        self.resource_cpu_utilization: float = 0.0

    # Reads go through the store so they see every worker's contribution;
    # writes happen only in observe/record, so name and delta live once.

    def _read(self, key: str) -> float:
        if self._counters is not None:
            return self._counters.get(key)
        return self._local.get(key, 0.0)

    def _add(self, key: str, delta: float) -> None:
        if self._counters is not None:
            self._counters.add(key, delta)
            return
        self._local[key] = self._local.get(key, 0.0) + delta

    def _labelled(self, prefix: str) -> dict[str, float]:
        source = self._counters.snapshot() if self._counters is not None else self._local
        return {key[len(prefix) :]: value for key, value in source.items() if key.startswith(prefix)}

    def _caller_series(self, prefix: str, caller_id: str) -> str:
        """The series a caller id is written under, bounded by an overflow fold.

        A NEW caller past the cap folds into one reserved overflow series so the
        label dimension stays bounded while bare and sum(labels) stay exact.
        """
        key = prefix + caller_id
        source = self._counters.snapshot() if self._counters is not None else self._local
        if key in source:
            return caller_id
        distinct = sum(1 for existing in source if existing.startswith(prefix))
        return _OVERFLOW_SERIES if distinct >= _MAX_CALLER_SERIES else caller_id

    @property
    def crawl_requests_total(self) -> float:
        return self._read(_CRAWL_REQUESTS)

    @property
    def crawl_requests_accepted_total(self) -> float:
        return self._read(_CRAWL_ACCEPTED)

    @property
    def crawl_results_total(self) -> float:
        return self._read(_CRAWL_RESULTS)

    @property
    def crawl_success_total(self) -> float:
        return self._read(_CRAWL_SUCCESS)

    @property
    def crawl_failure_total(self) -> float:
        return self._read(_CRAWL_FAILURE)

    @property
    def crawl_errors_total(self) -> float:
        return self._read(_CRAWL_ERRORS)

    @property
    def total_elapsed_ms(self) -> float:
        return self._read(_CRAWL_ELAPSED)

    @property
    def search_requests_total(self) -> float:
        return self._read(_SEARCH_REQUESTS)

    @property
    def search_results_total(self) -> float:
        return self._read(_SEARCH_RESULTS)

    @property
    def search_errors_total(self) -> float:
        return self._read(_SEARCH_ERRORS)

    @property
    def search_total_elapsed_ms(self) -> float:
        return self._read(_SEARCH_ELAPSED)

    @property
    def resource_throttle_batches_total(self) -> float:
        return self._read(_THROTTLE_BATCHES)

    @property
    def strategy_counts(self) -> dict[str, float]:
        return self._labelled(_STRATEGY_PREFIX)

    @property
    def classification_counts(self) -> dict[str, float]:
        return self._labelled(_CLASSIFICATION_PREFIX)

    @property
    def search_provider_counts(self) -> dict[str, float]:
        return self._labelled(_PROVIDER_PREFIX)

    @property
    def caller_counts(self) -> dict[str, float]:
        return self._labelled(_CALLER_PREFIX)

    @property
    def search_caller_counts(self) -> dict[str, float]:
        return self._labelled(_SEARCH_CALLER_PREFIX)

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
        self._add(_CRAWL_REQUESTS, 1)
        if caller_id:
            self._add(_CALLER_PREFIX + self._caller_series(_CALLER_PREFIX, caller_id), 1)
        if accepted:
            self._add(_CRAWL_ACCEPTED, 1)
        if results:
            self._add(_CRAWL_RESULTS, len(results))
            for result in results:
                if result.status == "success":
                    self._add(_CRAWL_SUCCESS, 1)
                else:
                    self._add(_CRAWL_FAILURE, 1)
                classification = result.classification
                if classification:
                    self._add(_CLASSIFICATION_PREFIX + str(classification), 1)
                strategy = result.selected_strategy
                if strategy:
                    self._add(_STRATEGY_PREFIX + str(strategy), 1)
        self._add(_CRAWL_ELAPSED, elapsed_ms)
        self._add(_CRAWL_ERRORS, errors)

    def observe_search(
        self,
        *,
        provider: str,
        results: int = 0,
        elapsed_ms: float = 0.0,
        errors: int = 0,
        caller_id: str = "",
    ) -> None:
        self._add(_SEARCH_REQUESTS, 1)
        self._add(_SEARCH_RESULTS, results)
        self._add(_SEARCH_ERRORS, errors)
        if provider:
            self._add(_PROVIDER_PREFIX + provider, results)
        if caller_id:
            self._add(_SEARCH_CALLER_PREFIX + self._caller_series(_SEARCH_CALLER_PREFIX, caller_id), 1)
        self._add(_SEARCH_ELAPSED, elapsed_ms)

    def record_throttle(self) -> None:
        """Count one batch shrunk by the admission gate; a method rather than
        ``+= 1`` on a property, because a setter cannot know its own delta without
        a read-then-write that loses increments under concurrent setters.
        """
        self._add(_THROTTLE_BATCHES, 1)

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
        return _exposition(self, caller_detail=caller_detail)


def _caller_lines(store: MetricsStore) -> list[str]:
    """Per-caller crawl request counts, sorted by caller id."""
    return [
        f'crawler_crawl_requests_by_caller_total{{caller="{caller}"}} {count}'
        for caller, count in sorted(store.caller_counts.items())
    ]


def _search_caller_lines(store: MetricsStore) -> list[str]:
    """Per-caller search request counts, sorted by caller id."""
    return [
        f'crawler_search_requests_by_caller_total{{caller="{caller}"}} {count}'
        for caller, count in sorted(store.search_caller_counts.items())
    ]


def _by_provider_lines(store: MetricsStore) -> list[str]:
    """The per-provider search-results family (own name, one label set)."""
    lines = [
        "# HELP crawler_search_results_by_provider_total Total web search results returned, per provider.",
        "# TYPE crawler_search_results_by_provider_total counter",
    ]
    lines.extend(
        f'crawler_search_results_by_provider_total{{provider="{provider}"}} {count}'
        for provider, count in sorted(store.search_provider_counts.items())
    )
    return lines


def _by_caller_lines(store: MetricsStore) -> list[str]:
    """The gated per-caller families (own names, one label set each)."""
    lines = [
        "# HELP crawler_crawl_requests_by_caller_total Crawl requests received, per caller id.",
        "# TYPE crawler_crawl_requests_by_caller_total counter",
    ]
    lines.extend(_caller_lines(store))
    lines.append("# HELP crawler_search_requests_by_caller_total Search requests received, per caller id.")
    lines.append("# TYPE crawler_search_requests_by_caller_total counter")
    lines.extend(_search_caller_lines(store))
    return lines


def _exposition(store: MetricsStore, *, caller_detail: bool) -> str:
    """Render the Prometheus text exposition for ``store``.

    A module function rather than a method: pure formatting over exposed values,
    keeping the oversized state object free of ~90 behaviourless lines.
    """
    lines: list[str] = []

    def counter(name: str, value: float, help_text: str) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} counter")
        lines.append(f"{name} {value}")

    counter("crawler_crawl_requests_total", store.crawl_requests_total, "Total crawl requests received.")
    counter(
        "crawler_crawl_requests_accepted_total",
        store.crawl_requests_accepted_total,
        "Total crawl requests accepted via webhook callback (HTTP 202).",
    )
    counter("crawler_crawl_results_total", store.crawl_results_total, "Total crawl results produced.")
    counter("crawler_crawl_success_total", store.crawl_success_total, "Total successful crawl results.")
    counter("crawler_crawl_failure_total", store.crawl_failure_total, "Total failed crawl results.")
    counter("crawler_crawl_errors_total", store.crawl_errors_total, "Total rejected/invalid crawl requests.")
    counter(
        "crawler_search_requests_total", store.search_requests_total, "Total web search requests received."
    )
    counter("crawler_search_results_total", store.search_results_total, "Total web search results returned.")
    counter("crawler_search_errors_total", store.search_errors_total, "Total failed web search requests.")
    lines.extend(_by_provider_lines(store))
    if caller_detail:
        lines.extend(_by_caller_lines(store))
    lines.append("# HELP crawler_search_seconds_total Total wall-clock time spent on web searches (seconds).")
    lines.append("# TYPE crawler_search_seconds_total counter")
    lines.append(f"crawler_search_seconds_total {store.search_total_elapsed_ms / 1000.0:.3f}")
    lines.append("# HELP crawler_crawl_seconds_total Total wall-clock time spent crawling (seconds).")
    lines.append("# TYPE crawler_crawl_seconds_total counter")
    lines.append(f"crawler_crawl_seconds_total {store.total_elapsed_ms / 1000.0:.3f}")
    lines.extend(
        f'crawler_strategy_results_total{{strategy="{strategy}"}} {count}'
        for strategy, count in sorted(store.strategy_counts.items())
    )
    lines.extend(
        f'crawler_result_classification_total{{classification="{classification}"}} {count}'
        for classification, count in sorted(store.classification_counts.items())
    )
    lines.extend(_gauge_and_throttle_lines(store))
    return "\n".join(lines) + "\n"


def _gauge_and_throttle_lines(store: MetricsStore) -> list[str]:
    """The gauges and throttle counter that trail the exposition."""
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
        f"crawler_resource_throttle_batches_total {store.resource_throttle_batches_total}",
    ]
    return lines


metrics = MetricsStore()


def get_metrics() -> MetricsStore:
    """Return the metrics singleton.

    Callers use this accessor rather than importing ``metrics`` so they observe
    the current store even if a test or harness swaps the singleton.
    """
    return metrics
