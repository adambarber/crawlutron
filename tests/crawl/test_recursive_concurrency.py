"""Throughput regression tests: recursive crawls must use the scheduler.

Before the batching fix the recursive branch awaited ``run_job`` one link at a
time, bypassing DomainScheduler entirely (no global/per-domain concurrency,
no rate limiting).
"""

import asyncio
from urllib.parse import urlparse

from app.crawl.engine import crawl_request
from app.models import HttpFetchResult
from app.settings import CrawlerSettings


def _page_html(path: str) -> str:
    # Only the seed page carries links; leaves are plain pages.
    if path == "/":
        anchors = "".join(f'<a href="/{c}">x</a>' for c in "abcde")
        return f"<html><body>seed {anchors}</body></html>"
    return f"<html><body>leaf {path}</body></html>"


async def test_recursive_crawl_fetches_links_concurrently(real_sleeps):
    """Discovered links are fetched concurrently, within the configured gates."""
    inflight = {"n": 0}
    max_inflight = {"n": 0}

    async def fetcher(url, headers, timeout, max_content_length=50 * 1024 * 1024):
        path = urlparse(url).path or "/"
        inflight["n"] += 1
        max_inflight["n"] = max(max_inflight["n"], inflight["n"])
        await asyncio.sleep(0.05)  # simulate network latency
        inflight["n"] -= 1
        return HttpFetchResult(
            url=url, final_url=url, status_code=200,
            headers={"content-type": "text/html"},
            body=_page_html(path).encode(), elapsed_ms=1,
        )

    payload = {
        "url": "http://example.com/",
        "policy": {"limits": {"max_depth": 1, "max_pages": 10}},
    }
    settings = CrawlerSettings.from_mapping({
        "BROWSER_CRAWLER_GLOBAL_CONCURRENCY": "8",
        # Same-host links are additionally bounded by the per-domain gate;
        # raise it so concurrency is observable for this single-domain site.
        "BROWSER_CRAWLER_PER_DOMAIN_CONCURRENCY": "4",
    })
    response = await crawl_request(payload, settings=settings, http_fetcher=fetcher)

    assert response["submitted_count"] == 1
    statuses = [r["status"] for r in response["results"]]
    assert all(s == "success" for s in statuses), statuses
    # 1 seed + 5 discovered leaf pages all crawled.
    assert len(response["results"]) == 6
    # Concurrency actually happened (sequential fetching yields max 1) ...
    assert max_inflight["n"] >= 2, f"recursive crawl was sequential: {max_inflight}"
    # ... but stayed within the configured per-domain gate.
    assert max_inflight["n"] <= 4, f"exceeded per-domain limit: {max_inflight}"
