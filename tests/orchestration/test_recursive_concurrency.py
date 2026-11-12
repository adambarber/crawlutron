"""Throughput regression tests: recursive crawls must use the scheduler.

Before the batching fix the recursive branch awaited one link at a time,
bypassing DomainScheduler entirely (no concurrency, no rate limiting).
"""

from crawlutron.config.settings import CrawlerSettings
from crawlutron.orchestration.engine import crawl_request
from tests._fetch_doubles import FetcherDouble
from tests._overlap_probe import OverlapProbe


def _page_html(path: str) -> str:
    # Only the seed page carries links; leaves are plain pages.
    if path == "/":
        anchors = "".join(f'<a href="/{c}">x</a>' for c in "abcde")
        return f"<html><body>seed {anchors}</body></html>"
    return f"<html><body>leaf {path}</body></html>"


async def test_recursive_crawl_fetches_links_concurrently():
    """Discovered links are fetched concurrently, within the configured gates. The probe's rendezvous holds
    four discovered-link fetchers in flight together; awaiting links one at a time never gathers four and
    the bounded wait expires, so the peak assert fails deterministically (no sleep-race).
    """
    probe = OverlapProbe(rendezvous_at=4)

    async def _answer(url, sent_headers, *_args):
        path = url.path or "/"
        return (200, _page_html(path), "text/html")

    fetcher = FetcherDouble(probe.wrap(_answer, participate=lambda url, *_args: url.path != "/"))
    fetcher.peak = probe.peak

    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 1, "max_pages": 10}},
    }
    settings = CrawlerSettings.from_mapping(
        {
            "CRAWLUTRON_GLOBAL_CONCURRENCY": "8",
            # Same-host links are additionally bounded by the per-domain gate; raise it so concurrency is
            # observable for this single-domain site.
            "CRAWLUTRON_PER_DOMAIN_CONCURRENCY": "4",
            # The scheduler's politeness delay would serialize the fetches this
            # test exists to observe; it drives the throttle off.
            "CRAWLUTRON_ADAPTIVE_THROTTLING": "0",
            "CRAWLUTRON_PER_DOMAIN_DELAY_MS": "0",
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
