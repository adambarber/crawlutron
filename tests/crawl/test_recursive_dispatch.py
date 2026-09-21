"""Direct unit tests for the extracted recursive dispatch (app.crawl.recursive).

These drive :func:`run_recursive_crawl` with fake closures/scheduler/spill so
the batch-sizing and admission-gate logic is covered without standing up the
full engine (the reason the dispatch was extracted from ``crawl_request``).
"""

import asyncio

from app.constants import BROWSER_HEADLESS
from app.crawl.recursive import run_recursive_crawl
from app.models import CrawlTask, DiscoveredLink
from app.scheduler import DomainScheduler
from tests._testkit import Spill, make_engine_result


class _Scheduler:
    def __init__(self):
        self.runs = 0

    async def run(self, jobs, worker, on_result=None, *, failed_result=None, **kwargs):
        self.runs += 1
        out = {}
        for job in jobs:
            try:
                out[job.index] = await worker(job)
            except Exception as error:
                if failed_result is None:
                    raise
                out[job.index] = failed_result(job, error)
        return out


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
        url=url,
        normalized_url=url,
        text="",
        rel=[],
        source_url="http://a/",
        host="a",
        is_same_host=True,
    )


async def _run(
    jobs,
    worker,
    gate=None,
    *,
    respect_robots=False,
    max_depth=2,
    max_pages=10,
    global_concurrency=4,
    seed_sitemaps=False,
    robots_allows=None,
    scheduler=None,
):
    if gate is None:
        gate = _Gate()
    spill = Spill()
    if scheduler is None:
        scheduler = _Scheduler()

    if robots_allows is None:

        async def robots_allows(url):
            return True

    await run_recursive_crawl(
        jobs=jobs,
        scheduler=scheduler,
        worker=worker,
        robots_allows=robots_allows,
        robots_disallowed=lambda job: make_engine_result(job.normalized_url),
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
        return make_engine_result(job.normalized_url)

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, gate = await _run(jobs, worker)
    assert len(spill.writes) == 1
    assert scheduler.runs == 1
    assert gate.waits == 0  # stop-the-world pause removed; admission is now per-batch weighted


async def test_expands_frontier_from_discovered_links():
    def _links_for(url):
        return [_link("http://a/1"), _link("http://a/2")] if url == "http://a/" else []

    async def worker(job):
        return make_engine_result(job.normalized_url, _links_for(job.normalized_url))

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, _ = await _run(jobs, worker)
    # root + /1 + /2
    assert len(spill.writes) == 3
    assert set(spill.writes[i].normalized_url for i in sorted(spill.writes)) == {
        "http://a/",
        "http://a/1",
        "http://a/2",
    }


async def test_gate_scale_shrinks_batch_but_still_crawls_all():
    # scale 0.0 forces batch_size to its floor (>=1); every discovered page must
    # still be crawled, just across more scheduler runs.
    async def worker(job):
        links = [_link("http://a/1"), _link("http://a/2")] if job.normalized_url == "http://a/" else []
        return make_engine_result(job.normalized_url, links)

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
        return make_engine_result(job.normalized_url, links)

    async def allows(url):
        return url != "http://a/blocked"

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, _, _ = await _run(jobs, worker, robots_allows=allows, respect_robots=True)
    assert seen == ["http://a/"]  # the blocked link was never crawled
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {"http://a/"}


async def test_robots_disallows_initial_job_as_skip():
    # An initial job that robots disallows is written as a skip, never crawled.
    seen = []

    async def worker(job):
        seen.append(job.normalized_url)
        return make_engine_result(job.normalized_url)

    async def allows(url):
        return url != "http://a/blocked"

    jobs = [
        CrawlTask(index=0, original_url="http://a/blocked", normalized_url="http://a/blocked", domain="a")
    ]
    spill, _, _ = await _run(jobs, worker, robots_allows=allows, respect_robots=True)
    assert seen == []  # worker never invoked
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {"http://a/blocked"}


# ---------------------------------------------------------------------------
# Result alignment (B1): the spill index must match the job that produced the
# result, even when tiers interleave or a job fails.
# ---------------------------------------------------------------------------
def _tiered_job(index: int, host: str) -> CrawlTask:
    url = f"http://{host}/"
    return CrawlTask(index=index, original_url=url, normalized_url=url, domain=host)


async def test_mixed_http_browser_batch_aligns_each_result_to_its_own_index():
    """A batch that interleaves HTTP and browser-pinned tasks must still write
    each result to its own job index.

    ``_pop_admitted`` emits tasks HTTP-first (tier order) while
    ``DomainScheduler.run`` reports results in index order with failures
    filtered out, so zipping the two mis-pairs every interleaved job.
    """
    scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
    scheduler.set_pinned_strategy("b1", BROWSER_HEADLESS)
    scheduler.set_pinned_strategy("b2", BROWSER_HEADLESS)

    # index/tier order deliberately interleaved: browser, http, browser, http
    jobs = [
        _tiered_job(0, "b1"),
        _tiered_job(1, "h1"),
        _tiered_job(2, "b2"),
        _tiered_job(3, "h2"),
    ]

    async def worker(job):
        # Result identity reflects the job that actually produced it.
        return make_engine_result(job.normalized_url)

    spill, _, _ = await _run(jobs, worker, scheduler=scheduler)

    assert set(spill.writes) == {0, 1, 2, 3}
    for index, result in spill.writes.items():
        expected = jobs[index].normalized_url
        assert result.normalized_url == expected, (
            f"spill[{index}] holds {result.normalized_url!r}, expected {expected!r}"
        )


async def test_failed_job_does_not_shift_other_results():
    """A job whose worker raises must not shift the pairing of later jobs, and
    must still receive its own failure envelope instead of vanishing."""
    scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
    jobs = [_tiered_job(i, f"h{i}") for i in range(4)]

    async def worker(job):
        if job.index == 1:
            raise RuntimeError("transient failure")
        return make_engine_result(job.normalized_url)

    spill, _, _ = await _run(jobs, worker, scheduler=scheduler)

    assert set(spill.writes) == {0, 1, 2, 3}
    for index, result in spill.writes.items():
        assert result.normalized_url == jobs[index].normalized_url, (
            f"spill[{index}] holds {result.normalized_url!r}, "
            f"expected {jobs[index].normalized_url!r}"
        )
    assert spill.writes[1].status == "failed"
