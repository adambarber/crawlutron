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
    strategy_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    classification_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    total_elapsed_ms: float = 0.0

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
        counter("crawler_crawl_errors_total", self.crawl_errors_total, "Total rejected/invalid crawl requests.")
        lines.append("# HELP crawler_crawl_seconds_total Total wall-clock time spent crawling (seconds).")
        lines.append("# TYPE crawler_crawl_seconds_total counter")
        lines.append(f"crawler_crawl_seconds_total {self.total_elapsed_ms / 1000.0:.3f}")
        for strategy, count in sorted(self.strategy_counts.items()):
            lines.append(f'crawler_strategy_results_total{{strategy="{strategy}"}} {count}')
        for classification, count in sorted(self.classification_counts.items()):
            lines.append(f'crawler_result_classification_total{{classification="{classification}"}} {count}')
        return "\n".join(lines) + "\n"


metrics = MetricsStore()
