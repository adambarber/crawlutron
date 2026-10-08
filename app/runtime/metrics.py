"""Prometheus-style metrics for the crawler service.

Counters are service totals and live in the shared coordination store, so
`/metrics` reports the service rather than whichever worker happened to answer
the request. `MetricsStore` started as a dataclass singleton whose counters
were plain integer increments reachable only from the process holding them, so
under `uvicorn --workers` the endpoint served one worker's numbers and called
the result total traffic. That matters more than a stale number: the admission
gate tunes itself from these numbers, so a reading that is a fraction of real
load looks like an idle service.

Gauges stay process-local on purpose. `resource_memory_utilization` and
`resource_cpu_utilization` measure a cgroup every worker shares, so each
worker's reading is equally valid and summing them would multiply a ratio by
the worker count -- an alert that fires on a healthy service and a signal whose
meaning depends on the deployment's worker count.

A store is optional. Without one the counters are a plain process-local dict,
which is the whole service in the single-process deployment that ships, and
keeps unit tests and offline debugging from needing a database.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING

from app.models.engine_crawl_result import EngineCrawlResult

if TYPE_CHECKING:
    from app.storage.coordination_counters import CoordinationCounters

# Store keys. The Prometheus names differ from these, so the mapping is written
# out once in `as_prometheus` rather than derived -- a derivation would have to
# encode the "crawler_" prefix and the _total suffix rules as string surgery.
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
_PAUSE_SECONDS = "resource_pause_seconds_total"

_STRATEGY_PREFIX = "strategy:"
_CLASSIFICATION_PREFIX = "classification:"
_PROVIDER_PREFIX = "search_provider:"

# Counter name -> the observable field callers read. Built once so a counter
# cannot be recorded under one name and read under another, which is the drift
# this class exists to prevent.
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
    _PAUSE_SECONDS: "resource_pause_seconds_total",
}

# Declared for the type checker and for readers: the counter fields are
# properties over `_COUNTERS`, not dataclass fields, so a rename cannot leave a
# field behind that nothing writes.
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

    # -- counter views -----------------------------------------------------
    # Reads go through the store so they see every worker's contribution.
    # Writes happen only in the observe/record methods below, so each counter's
    # name and its delta live in exactly one place.

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
    def resource_pause_seconds_total(self) -> float:
        return self._read(_PAUSE_SECONDS)

    @property
    def strategy_counts(self) -> dict[str, float]:
        return self._labelled(_STRATEGY_PREFIX)

    @property
    def classification_counts(self) -> dict[str, float]:
        return self._labelled(_CLASSIFICATION_PREFIX)

    @property
    def search_provider_counts(self) -> dict[str, float]:
        return self._labelled(_PROVIDER_PREFIX)

    # -- the only counter write paths --------------------------------------

    def observe_crawl(
        self,
        *,
        accepted: bool = False,
        results: Sequence[EngineCrawlResult] | None = None,
        elapsed_ms: float = 0.0,
        errors: int = 0,
    ) -> None:
        self._add(_CRAWL_REQUESTS, 1)
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
    ) -> None:
        self._add(_SEARCH_REQUESTS, 1)
        self._add(_SEARCH_RESULTS, results)
        self._add(_SEARCH_ERRORS, errors)
        if provider:
            self._add(_PROVIDER_PREFIX + provider, results)
        self._add(_SEARCH_ELAPSED, elapsed_ms)

    def record_throttle(self) -> None:
        """Count one batch shrunk by the admission gate.

        A method rather than a `+= 1` on a property, because a property setter
        cannot know its own delta without a read-then-write that loses
        increments when two workers set it at once.
        """
        self._add(_THROTTLE_BATCHES, 1)

    def record_pause(self, seconds: float) -> None:
        """Count wall-clock seconds dispatch waited for a saturated resource."""
        self._add(_PAUSE_SECONDS, seconds)

    @property
    def counters(self) -> CoordinationCounters | None:
        """The shared storage backing these counters, or None before the
        lifespan binds it. A reader in another worker only sees these totals
        once this is a store rather than this process's own dict."""
        return self._counters

    def bind_counters(self, counters: CoordinationCounters) -> None:
        """Attach shared storage mid-life, and import any counters already there.

        The lifespan attaches the store after the process is already running, so
        counters a sibling worker recorded before this one finished booting must
        be carried over rather than dropped -- otherwise worker 3's early
        requests vanish from the totals for the life of the process.
        """
        if self._counters is not None or counters.snapshot():
            self._counters = counters
            return
        carried = self._local
        self._counters = counters
        for key, value in carried.items():
            counters.add(key, value)
        self._local = {}

    def reset(self) -> None:
        """Zero every counter and gauge in place.

        Resets the shared store too: a reset that only cleared this process's
        memory would leave the next read unchanged, and an operator who clears
        counters after an incident expects the endpoint to agree.
        """
        if self._counters is not None:
            self._counters.reset()
        else:
            self._local.clear()
        self.resource_memory_utilization = 0.0
        self.resource_cpu_utilization = 0.0

    def as_prometheus(self) -> str:
        return _exposition(self)


def _exposition(store: MetricsStore) -> str:
    """Render the Prometheus text exposition for ``store``.

    A module function rather than a method: this is pure formatting over values
    the store already exposes, and keeping ~90 lines of it on the state object
    made MetricsStore the fourth oversized class in the tree without adding a
    single behaviour.
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
    for provider, count in sorted(store.search_provider_counts.items()):
        lines.append(f'crawler_search_results_total{{provider="{provider}"}} {count}')
    lines.append("# HELP crawler_search_seconds_total Total wall-clock time spent on web searches (seconds).")
    lines.append("# TYPE crawler_search_seconds_total counter")
    lines.append(f"crawler_search_seconds_total {store.search_total_elapsed_ms / 1000.0:.3f}")
    lines.append("# HELP crawler_crawl_seconds_total Total wall-clock time spent crawling (seconds).")
    lines.append("# TYPE crawler_crawl_seconds_total counter")
    lines.append(f"crawler_crawl_seconds_total {store.total_elapsed_ms / 1000.0:.3f}")
    for strategy, count in sorted(store.strategy_counts.items()):
        lines.append(f'crawler_strategy_results_total{{strategy="{strategy}"}} {count}')
    for classification, count in sorted(store.classification_counts.items()):
        lines.append(f'crawler_result_classification_total{{classification="{classification}"}} {count}')
    lines.append(
        "# HELP crawler_resource_memory_utilization Current memory usage as a fraction of the "
        "cgroup memory limit (or host RAM). >1 means over the hard ceiling."
    )
    lines.append("# TYPE crawler_resource_memory_utilization gauge")
    lines.append(f"crawler_resource_memory_utilization {store.resource_memory_utilization:.4f}")
    lines.append(
        "# HELP crawler_resource_cpu_utilization Current CPU utilization as a fraction of the "
        "cgroup CPU quota (or manual budget). >1 means over budget / being throttled."
    )
    lines.append("# TYPE crawler_resource_cpu_utilization gauge")
    lines.append(f"crawler_resource_cpu_utilization {store.resource_cpu_utilization:.4f}")
    lines.append(
        "# HELP crawler_resource_throttle_batches_total Crawl batches shrunk by resource admission "
        "control (memory and/or CPU pressure)."
    )
    lines.append("# TYPE crawler_resource_throttle_batches_total counter")
    lines.append(f"crawler_resource_throttle_batches_total {store.resource_throttle_batches_total}")
    lines.append(
        "# HELP crawler_resource_pause_seconds_total Wall-clock seconds dispatch was paused waiting "
        "for a saturated resource to drain."
    )
    lines.append("# TYPE crawler_resource_pause_seconds_total counter")
    lines.append(f"crawler_resource_pause_seconds_total {store.resource_pause_seconds_total:.3f}")
    return "\n".join(lines) + "\n"


metrics = MetricsStore()


def get_metrics() -> MetricsStore:
    """Return the metrics singleton.

    Callers should use this accessor instead of ``from app.runtime.metrics import
    metrics`` so they always observe the current store -- including if a test
    or harness swaps the singleton -- rather than a name bound at import time.
    """
    return metrics
