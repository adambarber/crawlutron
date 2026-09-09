"""Process-local Prometheus-style metrics for the crawler service (#16).

These counters are intentionally process-local (no external TSDB required).
The ``/metrics`` endpoint exposes them in the Prometheus text exposition
format so any scraper can pull them.
"""

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class MetricsStore:
    crawl_requests_total: int = 0
    crawl_requests_accepted_total: int = 0  # webhook (202) accepted requests
    crawl_results_total: int = 0
    crawl_success_total: int = 0
    crawl_failure_total: int = 0
    crawl_errors_total: int = 0
    # Resource admission control (#self-tuning): live utilization vs the cgroup
    # ceiling and throttle/pause counters, so operators can monitor the guard.
    resource_memory_utilization: float = 0.0
    resource_cpu_utilization: float = 0.0
    resource_throttle_batches_total: int = 0
    resource_pause_seconds_total: float = 0.0
    strategy_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    classification_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_elapsed_ms: float = 0.0
    # Web search (POST /v1/search): per-provider result counts so operators can
    # see which backend serves queries and whether any provider is erroring.
    search_requests_total: int = 0
    search_results_total: int = 0
    search_errors_total: int = 0
    search_provider_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    search_total_elapsed_ms: float = 0.0

    def reset(self) -> None:
        """Reinitialize every field in place to its default.

        Used between tests so the process-local store (a module-level singleton)
        does not leak counters/gauges across tests and make metric assertions
        order-dependent. Resets in place so existing ``from app.metrics import
        metrics`` references keep pointing at the same object.
        """
        fresh = MetricsStore()
        self.__dict__.update(fresh.__dict__)

    def observe_crawl(
        self,
        *,
        accepted: bool = False,
        results: list[dict[str, Any]] | None = None,
        elapsed_ms: float = 0.0,
        errors: int = 0,
    ) -> None:
        if accepted:
            self.crawl_requests_accepted_total += 1
        self.crawl_requests_total += 1
        if results:
            self.crawl_results_total += len(results)
            for result in results:
                if result.get("status") == "success":
                    self.crawl_success_total += 1
                else:
                    self.crawl_failure_total += 1
                classification = result.get("classification")
                if classification:
                    self.classification_counts[classification] += 1
                strategy = result.get("selected_strategy")
                if strategy:
                    self.strategy_counts[strategy] += 1
        self.total_elapsed_ms += elapsed_ms
        self.crawl_errors_total += errors

    def observe_search(
        self,
        *,
        provider: str,
        results: int = 0,
        elapsed_ms: float = 0.0,
        errors: int = 0,
    ) -> None:
        self.search_requests_total += 1
        self.search_results_total += results
        self.search_errors_total += errors
        if provider:
            self.search_provider_counts[provider] += results
        self.search_total_elapsed_ms += elapsed_ms

    def as_prometheus(self) -> str:
        lines: list[str] = []

        def counter(name: str, value: float, help_text: str) -> None:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} counter")
            lines.append(f"{name} {value}")

        counter("crawler_crawl_requests_total", self.crawl_requests_total, "Total crawl requests received.")
        counter(
            "crawler_crawl_requests_accepted_total",
            self.crawl_requests_accepted_total,
            "Total crawl requests accepted via webhook callback (HTTP 202).",
        )
        counter("crawler_crawl_results_total", self.crawl_results_total, "Total crawl results produced.")
        counter("crawler_crawl_success_total", self.crawl_success_total, "Total successful crawl results.")
        counter("crawler_crawl_failure_total", self.crawl_failure_total, "Total failed crawl results.")
        counter(
            "crawler_crawl_errors_total", self.crawl_errors_total, "Total rejected/invalid crawl requests."
        )
        counter(
            "crawler_search_requests_total", self.search_requests_total, "Total web search requests received."
        )
        counter(
            "crawler_search_results_total", self.search_results_total, "Total web search results returned."
        )
        counter("crawler_search_errors_total", self.search_errors_total, "Total failed web search requests.")
        for provider, count in sorted(self.search_provider_counts.items()):
            lines.append(f'crawler_search_results_total{{provider="{provider}"}} {count}')
        lines.append(
            "# HELP crawler_search_seconds_total Total wall-clock time spent on web searches (seconds)."
        )
        lines.append("# TYPE crawler_search_seconds_total counter")
        lines.append(f"crawler_search_seconds_total {self.search_total_elapsed_ms / 1000.0:.3f}")
        lines.append("# HELP crawler_crawl_seconds_total Total wall-clock time spent crawling (seconds).")
        lines.append("# TYPE crawler_crawl_seconds_total counter")
        lines.append(f"crawler_crawl_seconds_total {self.total_elapsed_ms / 1000.0:.3f}")
        for strategy, count in sorted(self.strategy_counts.items()):
            lines.append(f'crawler_strategy_results_total{{strategy="{strategy}"}} {count}')
        for classification, count in sorted(self.classification_counts.items()):
            lines.append(f'crawler_result_classification_total{{classification="{classification}"}} {count}')
        lines.append(
            "# HELP crawler_resource_memory_utilization Current memory usage as a fraction of the "
            "cgroup memory limit (or host RAM). >1 means over the hard ceiling."
        )
        lines.append("# TYPE crawler_resource_memory_utilization gauge")
        lines.append(f"crawler_resource_memory_utilization {self.resource_memory_utilization:.4f}")
        lines.append(
            "# HELP crawler_resource_cpu_utilization Current CPU utilization as a fraction of the "
            "cgroup CPU quota (or manual budget). >1 means over budget / being throttled."
        )
        lines.append("# TYPE crawler_resource_cpu_utilization gauge")
        lines.append(f"crawler_resource_cpu_utilization {self.resource_cpu_utilization:.4f}")
        lines.append(
            "# HELP crawler_resource_throttle_batches_total Crawl batches shrunk by resource admission "
            "control (memory and/or CPU pressure)."
        )
        lines.append("# TYPE crawler_resource_throttle_batches_total counter")
        lines.append(f"crawler_resource_throttle_batches_total {self.resource_throttle_batches_total}")
        lines.append(
            "# HELP crawler_resource_pause_seconds_total Wall-clock seconds dispatch was paused waiting "
            "for a saturated resource to drain."
        )
        lines.append("# TYPE crawler_resource_pause_seconds_total counter")
        lines.append(f"crawler_resource_pause_seconds_total {self.resource_pause_seconds_total:.3f}")
        return "\n".join(lines) + "\n"


metrics = MetricsStore()


def get_metrics() -> "MetricsStore":
    """Return the process-local metrics singleton.

    Callers should use this accessor instead of ``from app.metrics import
    metrics`` so they always observe the current store -- including if a test
    or harness swaps the singleton -- rather than a name bound at import time.
    """
    return metrics
