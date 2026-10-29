"""The /metrics exposition is pinned by an exact golden: a dropped line fails with a diff."""

from __future__ import annotations

from crawlutron.runtime.metrics import MetricsStore
from crawlutron.vocabulary.strategies import HTTP_DECLARED_CRAWLER
from tests._result_builders import engine_result

_CALLER_DETAIL_BLOCK = """\
# HELP crawler_crawl_requests_by_caller_total Crawl requests received, per caller id.
# TYPE crawler_crawl_requests_by_caller_total counter
crawler_crawl_requests_by_caller_total{caller="key-1"} 2.0
# HELP crawler_search_requests_by_caller_total Search requests received, per caller id.
# TYPE crawler_search_requests_by_caller_total counter
crawler_search_requests_by_caller_total{caller="key-1"} 1.0
"""

# Each counter family as its own HELP/TYPE/value triple, so no line exceeds the
# budget while the assembled golden still names every line in production order.
_COUNTS_BLOCK = (
    "# HELP crawler_crawl_requests_total Total crawl requests received.\n"
    "# TYPE crawler_crawl_requests_total counter\n"
    "crawler_crawl_requests_total 2.0\n"
    "# HELP crawler_crawl_requests_accepted_total Total crawl requests accepted via"
    " webhook callback (HTTP 202).\n"
    "# TYPE crawler_crawl_requests_accepted_total counter\n"
    "crawler_crawl_requests_accepted_total 1.0\n"
    "# HELP crawler_crawl_results_total Total crawl results produced.\n"
    "# TYPE crawler_crawl_results_total counter\n"
    "crawler_crawl_results_total 2.0\n"
    "# HELP crawler_crawl_success_total Total successful crawl results.\n"
    "# TYPE crawler_crawl_success_total counter\n"
    "crawler_crawl_success_total 1.0\n"
    "# HELP crawler_crawl_failure_total Total failed crawl results.\n"
    "# TYPE crawler_crawl_failure_total counter\n"
    "crawler_crawl_failure_total 1.0\n"
    "# HELP crawler_crawl_errors_total Total rejected/invalid crawl requests.\n"
    "# TYPE crawler_crawl_errors_total counter\n"
    "crawler_crawl_errors_total 2.0\n"
    "# HELP crawler_search_requests_total Total web search requests received.\n"
    "# TYPE crawler_search_requests_total counter\n"
    "crawler_search_requests_total 2.0\n"
    "# HELP crawler_search_results_total Total web search results returned.\n"
    "# TYPE crawler_search_results_total counter\n"
    "crawler_search_results_total 9.0\n"
    "# HELP crawler_search_errors_total Total failed web search requests.\n"
    "# TYPE crawler_search_errors_total counter\n"
    "crawler_search_errors_total 1.0\n"
    "# HELP crawler_extraction_doc_pool_unavailable_total Document-extraction acquisitions"
    " degraded: no pool worker was available within the budget.\n"
    "# TYPE crawler_extraction_doc_pool_unavailable_total counter\n"
    "crawler_extraction_doc_pool_unavailable_total 1.0\n"
)

_PROVIDER_BLOCK = """\
# HELP crawler_search_results_by_provider_total Total web search results returned, per provider.
# TYPE crawler_search_results_by_provider_total counter
crawler_search_results_by_provider_total{provider="brave"} 4.0
crawler_search_results_by_provider_total{provider="tavily"} 5.0
"""

_SECONDS_BLOCK = """\
# HELP crawler_search_seconds_total Total wall-clock time spent on web searches (seconds).
# TYPE crawler_search_seconds_total counter
crawler_search_seconds_total 2.500
# HELP crawler_crawl_seconds_total Total wall-clock time spent crawling (seconds).
# TYPE crawler_crawl_seconds_total counter
crawler_crawl_seconds_total 12.500
"""

_LABELLED_BLOCK = """\
crawler_strategy_results_total{strategy="HTTP_DECLARED_CRAWLER"} 1.0
crawler_result_classification_total{classification="dns_error"} 1.0
crawler_result_classification_total{classification="rendered"} 1.0
"""

_GAUGES_BLOCK = (
    "# HELP crawler_resource_memory_utilization Current memory usage as a fraction of the "
    "cgroup memory limit (or host RAM). >1 means over the hard ceiling.\n"
    "# TYPE crawler_resource_memory_utilization gauge\n"
    "crawler_resource_memory_utilization 0.2500\n"
)

_CPU_GAUGE_BLOCK = (
    "# HELP crawler_resource_cpu_utilization Current CPU utilization as a fraction of the "
    "cgroup CPU quota (or manual budget). >1 means over budget / being throttled.\n"
    "# TYPE crawler_resource_cpu_utilization gauge\n"
    "crawler_resource_cpu_utilization 0.5000\n"
)

_THROTTLE_BLOCK = (
    "# HELP crawler_resource_throttle_batches_total Crawl batches shrunk by resource "
    "admission control (memory and/or CPU pressure).\n"
    "# TYPE crawler_resource_throttle_batches_total counter\n"
    "crawler_resource_throttle_batches_total 1.0\n"
)


def _assemble_golden() -> str:
    return (
        _COUNTS_BLOCK
        + _PROVIDER_BLOCK
        + _CALLER_DETAIL_BLOCK
        + _SECONDS_BLOCK
        + _LABELLED_BLOCK
        + _GAUGES_BLOCK
        + _CPU_GAUGE_BLOCK
        + _THROTTLE_BLOCK
    )


_GOLDEN_CALLER_DETAIL = _assemble_golden()
_GOLDEN_WITHOUT_CALLER_DETAIL = _GOLDEN_CALLER_DETAIL.replace(_CALLER_DETAIL_BLOCK, "")


def _observed_store() -> MetricsStore:
    """One store after a known, deterministic sequence of observations."""
    store = MetricsStore()
    store.resource_memory_utilization = 0.25
    store.resource_cpu_utilization = 0.5
    store.observe_crawl(
        accepted=True,
        results=[
            engine_result("http://example.com/a", "rendered", selected_strategy=HTTP_DECLARED_CRAWLER),
            engine_result("http://example.com/b", "dns_error"),
        ],
        elapsed_ms=12_500,
        errors=2,
        caller_id="key-1",
    )
    store.observe_crawl(caller_id="key-1")
    store.observe_search(provider="tavily", results=5, elapsed_ms=2_500, caller_id="key-1")
    store.observe_search(provider="brave", results=4, errors=1)
    store.record_doc_pool_unavailable()
    store.record_throttle()
    return store


def test_the_full_exposition_matches_the_golden():
    """Any dropped, renamed, reordered or reformatted line is a failure with a diff.

    The whole exposition is the golden, both caller_detail states, so the
    labelled families and the HELP/TYPE lines nothing substring-checked are held.
    """
    store = _observed_store()
    assert store.as_prometheus(caller_detail=True) == _GOLDEN_CALLER_DETAIL
    assert store.as_prometheus(caller_detail=False) == _GOLDEN_WITHOUT_CALLER_DETAIL


# -- the declared line contract (#485): the golden pins the exact bytes;
# this section pins the declaration itself, so an undeclared family fails.


def test_every_plain_counter_family_is_declared_in_the_registry():
    """Each bare counter series in the golden is a registry entry, in golden order."""
    from crawlutron.runtime.metrics import COUNTER_FAMILIES

    expected = [
        "crawler_crawl_requests_total",
        "crawler_crawl_requests_accepted_total",
        "crawler_crawl_results_total",
        "crawler_crawl_success_total",
        "crawler_crawl_failure_total",
        "crawler_crawl_errors_total",
        "crawler_search_requests_total",
        "crawler_search_results_total",
        "crawler_search_errors_total",
        "crawler_extraction_doc_pool_unavailable_total",
        "crawler_search_seconds_total",
        "crawler_crawl_seconds_total",
        "crawler_resource_throttle_batches_total",
    ]
    assert [family.name for family in COUNTER_FAMILIES] == expected
