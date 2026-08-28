"""Direct unit tests for the extracted recursive dispatch (app.crawl.recursive).

These drive :func:`run_recursive_crawl` with fake closures/scheduler/spill so
the batch-sizing and admission-gate logic is covered without standing up the
full engine (the reason the dispatch was extracted from ``crawl_request``).
"""

import asyncio

import pytest

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


class _Spill:
    def __init__(self):
        self.writes = {}

    def write(self, index, result):
        self.writes[index] = result

    def load_ordered(self):
        return [self.writes[i] for i in sorted(self.writes)]


class _Scheduler:
    def __init__(self):
        self.runs = 0

    async def run(self, jobs, worker):
        self.runs += 1
        return [await worker(j) for j in jobs]


class _Gate:
    def __init__(self, scale=1.0):
        self.waits = 0
        self.scale = scale

    async def wait_while_saturated(self):
        self.waits += 1

    def batch_scale_factor(self):
        return self.scale

    def memory_ratio(self):
        return 0.0

    def cpu_ratio(self):
        return 0.0


def _link(url: str) -> DiscoveredLink:
    return DiscoveredLink(
        url=url, normalized_url=url, text="", rel=[], source_url="http://a/",
        host="a", is_same_host=True,
    )


async def _run(jobs, worker, gate=None, *, respect_robots=False, max_depth=2,
               max_pages=10, global_concurrency=4, seed_sitemaps=False):
    if gate is None:
        gate = _Gate()
    spill = _Spill()
    scheduler = _Scheduler()

    async def robots_allows(url):
        return True

    await run_recursive_crawl(
        jobs=jobs,
        scheduler=scheduler,
        worker=worker,
        robots_allows=robots_allows,
        robots_disallowed=lambda job: _make_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]),
        spill=spill,
        shutdown_event=asyncio.Event(),
        max_depth=max_depth,
        max_pages=max_pages,
        respect_robots=respect_robots,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=global_concurrency,
        seed_sitemaps=seed_sitemaps,
        resource_gate=gate,
    )
    return spill, scheduler, gate


async def test_dispatches_single_page_and_consults_gate():
    async def worker(job):
        return _make_result(job.normalized_url)

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, gate = await _run(jobs, worker)
    assert len(spill.writes) == 1
    assert scheduler.runs == 1
    assert gate.waits == 0  # stop-the-world pause removed; admission is now per-batch weighted


async def test_expands_frontier_from_discovered_links():
    def _links_for(url):
        return [_link("http://a/1"), _link("http://a/2")] if url == "http://a/" else []

    async def worker(job):
        return _make_result(job.normalized_url, _links_for(job.normalized_url))

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, _ = await _run(jobs, worker)
    # root + /1 + /2
    assert len(spill.writes) == 3
    assert set(spill.writes[i].normalized_url for i in sorted(spill.writes)) == {
        "http://a/", "http://a/1", "http://a/2",
    }


async def test_gate_scale_shrinks_batch_but_still_crawls_all():
    # scale 0.0 forces batch_size to its floor (>=1); every discovered page must
    # still be crawled, just across more scheduler runs.
    async def worker(job):
        links = [_link("http://a/1"), _link("http://a/2")] if job.normalized_url == "http://a/" else []
        return _make_result(job.normalized_url, links)

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, _ = await _run(jobs, worker, gate=_Gate(scale=0.0))
    assert len(spill.writes) == 3
    assert scheduler.runs >= 2  # shrunk batches => multiple scheduler runs


async def test_robots_disallows_discovered_link_silently():
    # A discovered link that robots disallows is neither crawled nor written.
    seen = []

    async def worker(job):
        seen.append(job.normalized_url)
        links = [_link("http://a/blocked")] if job.normalized_url == "http://a/" else []
        return _make_result(job.normalized_url, links)

    async def robots_allows(url):
        return url != "http://a/blocked"

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill = _Spill()
    scheduler = _Scheduler()
    await run_recursive_crawl(
        jobs=jobs, scheduler=scheduler, worker=worker, robots_allows=robots_allows,
        robots_disallowed=lambda job: _make_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]), spill=spill,
        shutdown_event=asyncio.Event(), max_depth=2, max_pages=10, respect_robots=True,
        include_html=True, want_document_bodies=False, scope_pattern=None, global_concurrency=4,
        seed_sitemaps=False, resource_gate=_Gate(),
    )
    assert seen == ["http://a/"]  # the blocked link was never crawled
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {"http://a/"}


async def test_robots_disallows_initial_job_as_skip():
    # An initial job that robots disallows is written as a skip, never crawled.
    seen = []

    async def worker(job):
        seen.append(job.normalized_url)
        return _make_result(job.normalized_url)

    async def robots_allows(url):
        return url != "http://a/blocked"

    jobs = [CrawlTask(index=0, original_url="http://a/blocked", normalized_url="http://a/blocked", domain="a")]
    spill = _Spill()
    scheduler = _Scheduler()
    await run_recursive_crawl(
        jobs=jobs, scheduler=scheduler, worker=worker, robots_allows=robots_allows,
        robots_disallowed=lambda job: _make_result(job.normalized_url),
        sitemap_seeds=lambda: asyncio.sleep(0, result=[]), spill=spill,
        shutdown_event=asyncio.Event(), max_depth=2, max_pages=10, respect_robots=True,
        include_html=True, want_document_bodies=False, scope_pattern=None, global_concurrency=4,
        seed_sitemaps=False, resource_gate=_Gate(),
    )
    assert seen == []  # worker never invoked
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {"http://a/blocked"}
