"""Perf item 1 (RED): robots checks must run concurrently, not sequentially.

``run_recursive_crawl`` (and the engine's non-recursive branch) await
``robots_allows``/``robots_crawl_delay`` one URL at a time, so a batch of N
URLs pays N sequential robots round-trips. These tests pin the concurrent
contract: overlapping checks must be observable via an in-flight counter.
"""

import asyncio

from app.crawl.recursive import run_recursive_crawl
from app.models import CrawlTask, DiscoveredLink, EngineCrawlResult


def _make_result(url: str, links=()):
    return EngineCrawlResult(
        original_url=url,
        normalized_url=url,
        final_url=url,
        status="success",
        classification="rendered",
        selected_strategy="HTTP_DECLARED_CRAWLER",
        attempted_strategies=[],
        http_status=200,
        headers={},
        content_type="text/html",
        html="<html></html>",
        text="t",
        extraction_method="http_html",
        quality_score=1.0,
        discovered_links=list(links),
    )


def _make_job(index: int, host: str) -> CrawlTask:
    url = f"http://{host}/page{index}"
    return CrawlTask(index=index, original_url=url, normalized_url=url, domain=host)


class _Spill:
    def __init__(self):
        self.writes = {}

    def write(self, index, result):
        self.writes[index] = result

    def load_ordered(self):
        return [self.writes[i] for i in sorted(self.writes)]


class _Scheduler:
    async def run(self, jobs, worker):
        return [await worker(j) for j in jobs]

    def get_pinned_strategy(self, domain):
        return None

    def set_domain_min_delay(self, domain, delay):
        return None


class _Gate:
    def batch_scale_factor(self):
        return 1.0

    def memory_ratio(self):
        return 0.0

    def cpu_ratio(self):
        return 0.0


def _tracker(delay=0.02):
    """Robots closure pair tracking max in-flight overlap."""
    state = {"in_flight": 0, "max_in_flight": 0, "calls": 0}

    async def _guarded():
        state["in_flight"] += 1
        state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
        try:
            await asyncio.sleep(delay)
        finally:
            state["in_flight"] -= 1

    async def allows(url):
        state["calls"] += 1
        await _guarded()
        return True

    async def crawl_delay(url):
        await _guarded()
        return None

    return allows, crawl_delay, state


async def _drive(jobs, worker, allows, crawl_delay, **kwargs):
    spill = _Spill()
    await run_recursive_crawl(
        jobs=jobs,
        scheduler=_Scheduler(),
        worker=worker,
        robots_allows=allows,
        robots_crawl_delay=crawl_delay,
        robots_disallowed=lambda job: _make_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]),
        spill=spill,
        shutdown_event=asyncio.Event(),
        max_depth=kwargs.get("max_depth", 0),
        max_pages=kwargs.get("max_pages", 50),
        respect_robots=True,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=kwargs.get("global_concurrency", 4),
        seed_sitemaps=False,
        resource_gate=_Gate(),
        admission=None,
    )
    return spill


async def test_recursive_batch_robots_checks_run_concurrently(real_sleeps):
    """Five seed URLs must be robots-screened with overlap, not one-by-one."""
    allows, crawl_delay, state = _tracker()
    jobs = [_make_job(i, f"host{i}.example") for i in range(5)]

    async def worker(job):
        return _make_result(job.normalized_url)

    await _drive(jobs, worker, allows, crawl_delay)
    assert state["calls"] == 5
    assert state["max_in_flight"] == 5


async def test_recursive_link_robots_checks_run_concurrently(real_sleeps):
    """Discovered links of one page must be robots-screened with overlap."""
    allows, crawl_delay, state = _tracker()
    links = [
        DiscoveredLink(
            url=f"http://link{i}.example/",
            normalized_url=f"http://link{i}.example/",
            text="",
            rel=[],
            source_url="http://seed.example/",
            host=f"link{i}.example",
            is_same_host=False,
        )
        for i in range(5)
    ]
    jobs = [_make_job(0, "seed.example")]

    async def worker(job):
        if job.normalized_url == "http://seed.example/page0":
            return _make_result(job.normalized_url, links=links)
        return _make_result(job.normalized_url)

    await _drive(jobs, worker, allows, crawl_delay, max_depth=1)
    assert state["calls"] >= 6  # seed batch screen + 5 discovered links
    assert state["max_in_flight"] == 5


async def test_nonrecursive_robots_checks_run_concurrently(real_sleeps, monkeypatch):
    """The engine's non-recursive branch must screen its batch with overlap."""
    from app.crawl.engine import crawl_request

    state = {"in_flight": 0, "max_in_flight": 0}

    async def fake_fetcher(url, headers, timeout_seconds, max_content_length=0):
        if url.endswith("/robots.txt"):
            state["in_flight"] += 1
            state["max_in_flight"] = max(state["max_in_flight"], state["in_flight"])
            try:
                await asyncio.sleep(0.02)
            finally:
                state["in_flight"] -= 1

            class _Resp:
                status_code = 200
                headers = {}
                body = b"User-agent: *\nDisallow:\n"
                final_url = url
                elapsed_ms = 1

            return _Resp()

        class _Resp:
            status_code = 200
            headers = {"content-type": "text/html"}
            body = b"<html><body>hello world, enough text to render</body></html>"
            final_url = url
            elapsed_ms = 1

        return _Resp()

    urls = [f"http://nc{i}.example/page" for i in range(5)]
    result = await crawl_request(
        {"urls": urls},
        http_fetcher=fake_fetcher,
        cffi_fetcher=fake_fetcher,
        browser_worker=None,
    )
    assert len(result["results"]) == 5
    assert state["max_in_flight"] == 5


async def test_gather_preserves_order_and_skips_delay_when_disallowed():
    """Order is input order; disallowed URLs never pay for a crawl-delay fetch."""
    from app.crawl.recursive import gather_robots_decisions

    delay_calls: list[str] = []

    async def allows(url):
        return url != "http://b.example/"

    async def crawl_delay(url):
        delay_calls.append(url)
        return 1.5

    out = await gather_robots_decisions(
        ["http://a.example/", "http://b.example/", "http://c.example/"], allows, crawl_delay
    )
    assert out == [(True, 1.5), (False, None), (True, 1.5)]
    assert delay_calls == ["http://a.example/", "http://c.example/"]


async def test_recursive_screen_no_robots_calls_when_disrespected():
    """respect_robots=False must not touch the robots closures at all."""
    calls: list[str] = []

    async def allows(url):
        calls.append(url)
        return True

    jobs = [_make_job(i, f"nr{i}.example") for i in range(3)]

    async def worker(job):
        return _make_result(job.normalized_url)

    spill = _Spill()
    await run_recursive_crawl(
        jobs=jobs,
        scheduler=_Scheduler(),
        worker=worker,
        robots_allows=allows,
        robots_crawl_delay=None,
        robots_disallowed=lambda job: _make_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]),
        spill=spill,
        shutdown_event=asyncio.Event(),
        max_depth=0,
        max_pages=50,
        respect_robots=False,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=4,
        seed_sitemaps=False,
        resource_gate=_Gate(),
        admission=None,
    )
    assert calls == []
    assert len(spill.writes) == 3


async def test_recursive_batch_duplicate_url_crawled_once():
    """A URL repeated inside one batch keeps the old visit-once semantics."""
    allows, crawl_delay, state = _tracker(delay=0)
    jobs = [_make_job(0, "dup.example"), _make_job(0, "dup.example")]
    seen: list[str] = []

    async def worker(job):
        seen.append(job.normalized_url)
        return _make_result(job.normalized_url)

    await _drive(jobs, worker, allows, crawl_delay)
    assert seen == ["http://dup.example/page0"]
    assert state["calls"] == 1
