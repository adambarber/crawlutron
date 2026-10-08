"""Throughput regression tests: recursive crawls must use the scheduler.

Before the batching fix the recursive branch awaited ``run_job`` one link at a
time, bypassing DomainScheduler entirely (no global/per-domain concurrency,
no rate limiting).
"""

from app.config.settings import CrawlerSettings
from app.orchestration.engine import crawl_request
from tests._fetcher_factories import concurrent_fetcher


def _page_html(path: str) -> str:
    # Only the seed page carries links; leaves are plain pages.
    if path == "/":
        anchors = "".join(f'<a href="/{c}">x</a>' for c in "abcde")
        return f"<html><body>seed {anchors}</body></html>"
    return f"<html><body>leaf {path}</body></html>"


async def test_recursive_crawl_fetches_links_concurrently():
    """Discovered links are fetched concurrently, within the configured gates.

    The overlap window is a scheduling point, not a duration: the fake fetcher
    yields once to the event loop mid-fetch, so every link the engine is willing
    to have in flight is in flight at the same time. A crawl that awaited the
    links one at a time would hold the harness's overlap peak at 1 and fail the lower
    bound, so the assertion is not weakened by removing the simulated network
    latency -- it is measuring the same thing with a cheaper instrument.
    """

    def respond(url, headers):
        path = url.path or "/"
        return (200, _page_html(path), "text/html")

    # The harness owns the overlap counter and the scheduling point that makes
    # overlap observable; this test owns what each path answers.
    fetcher = concurrent_fetcher(respond)

    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 1, "max_pages": 10}},
    }
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_GLOBAL_CONCURRENCY": "8",
            # Same-host links are additionally bounded by the per-domain gate;
            # raise it so concurrency is observable for this single-domain site.
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "4",
        }
    )
    response = await crawl_request(payload, settings=settings, http_fetcher=fetcher)

    assert response["submitted_count"] == 1
    statuses = [r.status for r in response["results"]]
    assert all(s == "success" for s in statuses), statuses
    # 1 seed + 5 discovered leaf pages all crawled.
    assert len(response["results"]) == 6
    # Concurrency actually happened (sequential fetching yields max 1) ...
    assert fetcher.peak() >= 2, f"recursive crawl was sequential: {fetcher.peak()}"
    # ... but stayed within the configured per-domain gate.
    assert fetcher.peak() <= 4, f"exceeded per-domain limit: {fetcher.peak()}"
