"""Throughput regression tests: recursive crawls must use the scheduler.

Before the batching fix the recursive branch awaited one link at a time,
bypassing DomainScheduler entirely (no concurrency, no rate limiting).
"""

import asyncio

from crawlutron.config.settings import CrawlerSettings
from crawlutron.orchestration.engine import crawl_request
from tests._fetch_doubles import FetcherDouble


def _page_html(path: str) -> str:
    # Only the seed page carries links; leaves are plain pages.
    if path == "/":
        anchors = "".join(f'<a href="/{c}">x</a>' for c in "abcde")
        return f"<html><body>seed {anchors}</body></html>"
    return f"<html><body>leaf {path}</body></html>"


async def test_recursive_crawl_fetches_links_concurrently():
    """Discovered links are fetched concurrently, within the configured gates.

    The fetcher yields once mid-fetch, so every in-flight link is in flight at
    once; awaiting them one at a time holds the peak at 1 and fails the bound.
    """

    def respond(url, headers):
        path = url.path or "/"
        return (200, _page_html(path), "text/html")

    # Each fetch parks on its own event pair: entering marks the URL active, and the peak is how many marks
    # were outstanding at the widest moment, which one-at-a-time scheduling holds at 1.
    _live: set[str] = set()
    _widest = {"n": 0}

    async def _overlap_answer(url, sent_headers, *_args):
        _live.add(url.raw)
        _widest["n"] = max(_widest["n"], len(_live))
        try:
            await asyncio.sleep(0)
            return respond(url, sent_headers)
        finally:
            _live.discard(url.raw)

    fetcher = FetcherDouble(_overlap_answer)
    fetcher.peak = lambda: _widest["n"]

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
