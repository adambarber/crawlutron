"""`/metrics` must report the service, not whichever worker answered.

MetricsStore is a dataclass singleton. Every counter is a plain integer
incremented under no lock at all, and the store is reachable only from the
process holding it -- so under `uvicorn --workers` the endpoint served one
worker's numbers and called the result total traffic. That matters more than
usual here because the admission gate tunes itself *from these numbers*: a
scraper or operator reading a fraction of real load sees a service that looks
idle.

The counter/gauge split is the design, and it is why this is not "sum
everything": `resource_memory_utilization` and `resource_cpu_utilization`
measure a cgroup that every worker shares, so they are the same reading
whichever worker reports it. Summing them would multiply a ratio by the
worker count and report a utilization no process is experiencing. Counters are
monotone and additive and must be summed; gauges are instantaneous and must
not be.

No multiprocessing here: two `CoordinationCounters` on one path are exactly
what two worker processes hold, which is how the counter store's own tests
prove the same property.
"""

from app.constants import HTTP_DECLARED_CRAWLER
from app.crawl.coordination_counters import CoordinationCounters
from app.metrics import MetricsStore
from tests._result_builders import engine_result


def _observe_one_success(metrics: MetricsStore) -> None:
    """One successful result recorded against `metrics`.

    Named because five tests in this file need exactly this and the call itself
    is a declared record now -- ``results`` is a sequence of engine results, so
    spelling the same two-line call five times is five chances for one of them to
    drift into a dict and test nothing.
    """
    metrics.observe_crawl(results=[engine_result("http://example.com/a", "rendered")])


def _two_workers(path):
    return MetricsStore(counters=CoordinationCounters(path)), MetricsStore(
        counters=CoordinationCounters(path)
    )


def test_counters_are_summed_across_workers(tmp_path):
    """The defect, stated as a test."""
    first, second = _two_workers(tmp_path / "coordination.db")

    _observe_one_success(first)
    _observe_one_success(second)

    # Each worker alone saw one result; the endpoint must report two.
    assert "crawler_crawl_results_total 2.0" in first.as_prometheus()


def test_a_worker_reports_totals_it_did_not_produce(tmp_path):
    """Reading one worker must still show the whole service.

    Otherwise a scraper gets a different answer depending on which worker the
    load balancer picked, which is worse than a stale number: it is an answer
    that looks live.
    """
    first, second = _two_workers(tmp_path / "coordination.db")
    second.observe_search(provider="tavily", results=7)

    assert "crawler_search_results_total 7.0" in first.as_prometheus()


def test_labelled_series_are_summed_not_overwritten(tmp_path):
    """Strategy and classification counts are labels on one series.

    Two workers each using the same strategy must not overwrite each other's
    contribution, which is the same lost-update shape the cookie jar had.
    """
    first, second = _two_workers(tmp_path / "coordination.db")
    named = engine_result("http://example.com/a", "rendered", selected_strategy=HTTP_DECLARED_CRAWLER)
    first.observe_crawl(results=[named])
    second.observe_crawl(results=[named])

    assert (
        f'crawler_strategy_results_total{{strategy="{HTTP_DECLARED_CRAWLER}"}} 2.0' in first.as_prometheus()
    )


def test_gauges_are_reported_per_worker_and_never_summed(tmp_path):
    """A shared cgroup reading is the same in every worker.

    Summing the two would report 2.0 when neither process is over budget --
    an alert that fires on a healthy service and a signal that stops meaning
    anything once the worker count changes.
    """
    first, second = _two_workers(tmp_path / "coordination.db")
    first.resource_memory_utilization = 0.25
    second.resource_memory_utilization = 0.25

    text = first.as_prometheus()
    assert "crawler_resource_memory_utilization 0.2500" in text
    assert "crawler_resource_memory_utilization 0.5000" not in text


def test_a_worker_with_no_shared_store_still_reports_its_own_counters():
    """Single-process operation must not require a database.

    The store is what makes the numbers a service total; without it they are
    one process's, which is the whole service in the deployment that ships.
    """
    metrics = MetricsStore()
    metrics.observe_crawl(results=[engine_result("http://example.com/a", "rendered")])

    assert "crawler_crawl_results_total 1.0" in metrics.as_prometheus()


def test_reset_clears_the_shared_store_not_just_local_fields(tmp_path):
    """A reset that only cleared memory would leave the next read unchanged.

    Test isolation depends on it, and so does an operator who resets counters
    after an incident and expects the endpoint to agree.
    """
    first, second = _two_workers(tmp_path / "coordination.db")
    _observe_one_success(first)
    _observe_one_success(second)

    first.reset()

    assert "crawler_crawl_results_total 0.0" in second.as_prometheus()
