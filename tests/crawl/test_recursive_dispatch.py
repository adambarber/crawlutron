"""Direct unit tests for the extracted recursive dispatch (app.crawl.recursive).

These drive :func:`run_recursive_crawl` with fake closures/scheduler/spill so
the batch-sizing and admission-gate logic is covered without standing up the
full engine (the reason the dispatch was extracted from ``crawl_request``).
"""

import pytest

from app.constants import BROWSER_HEADLESS
from app.models import CrawlTask, DiscoveredLink
from app.scheduler import DomainScheduler
from tests._recursive_harnesses import ContentionFreeGate, drive_recursive_crawl, make_crawl_task
from tests._result_builders import engine_result, make_engine_result


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


async def _allows(url):
    return True


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
    """These tests default to ``max_depth=2``; the harness default is 0.

    A dispatch test wants the frontier to actually expand, and the bound under
    test is passed explicitly, so the depth default is the only reason this
    wrapper exists at all.
    """
    return await drive_recursive_crawl(
        jobs,
        worker,
        allows=robots_allows if robots_allows is not None else _allows,
        gate=gate,
        scheduler=scheduler,
        respect_robots=respect_robots,
        max_depth=max_depth,
        max_pages=max_pages,
        global_concurrency=global_concurrency,
        seed_sitemaps=seed_sitemaps,
    )


@pytest.mark.parametrize(
    ("keyword", "value"),
    (("max_dept", 2), ("retired_keyword", True)),
    ids=("misspelled-at-the-call-site", "dropped-from-production"),
)
async def test_an_override_production_does_not_take_is_rejected(keyword, value):
    """The harness forwards through ``**overrides``; a keyword it cannot bind must not pass.

    This is the whole reason the harness is not allowed to be a plain
    ``**kwargs`` catch-all, and the reason it cannot be. Before the signature was
    read from production, every keyword was named in the harness's own signature
    and Python itself raised on a misspelling. Reading the contract at runtime
    keeps that property only if the binding is enforced, so this asserts the
    enforcement rather than trusting it -- a rejected keyword names itself
    instead of becoming an argument production never received.

    The two cases fail differently and both matter. A misspelling is a caller
    mistake, and silently dropping it means a test ran against a default nobody
    chose -- a ``max_dept`` that never bounded anything. A keyword production has
    dropped is worse: the harness would accept it, hand it over, and either pass a
    default the caller chose nothing about or mask the removal entirely.
    """

    async def worker(job):
        return make_engine_result(job.normalized_url)

    with pytest.raises(TypeError, match=keyword):
        await drive_recursive_crawl([make_crawl_task(0, "a")], worker, allows=_allows, **{keyword: value})


async def test_a_real_override_still_reaches_production():
    """Binding validates names, so the value path has to be proven separately.

    A harness that rejected every override would satisfy both tests above. This
    one drives a real bound through and reads it back off the crawl, which is the
    only evidence that ``overrides`` is a funnel rather than a wall.
    """

    async def worker(job):
        return make_engine_result(job.normalized_url)

    jobs = [make_crawl_task(0, "a"), make_crawl_task(1, "a"), make_crawl_task(2, "a")]
    spill, _scheduler, _gate = await drive_recursive_crawl(
        jobs, worker, allows=_allows, max_pages=2, respect_robots=False
    )
    assert len(spill.writes) == 2


async def test_dispatches_single_page_and_consults_gate():
    async def worker(job):
        return make_engine_result(job.normalized_url)

    jobs = [CrawlTask(index=0, original_url="http://a/", normalized_url="http://a/", domain="a")]
    spill, scheduler, gate = await _run(jobs, worker)
    assert len(spill.writes) == 1
    assert scheduler.runs == 1
    # Admission is per-batch weighted, not a stop-the-world pause: the gate is
    # consulted for a scale and the batch is dispatched anyway. Asserting the
    # consultation (rather than the absence of a wait that production never
    # performs) is the part that can actually fail.
    assert gate.consultations >= 1
    assert gate.scale == 1.0


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
    spill, scheduler, _ = await _run(jobs, worker, gate=ContentionFreeGate(0.0))
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


async def test_robots_disallows_initial_job_as_skip(recording_worker):
    # An initial job that robots disallows is written as a skip, never crawled.
    seen, worker = recording_worker

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
def assert_spill_keeps_job_pairing(spill, jobs) -> None:
    """Every job's result is in its own spill slot, under its own URL.

    The invariant the whole dispatch tier exists to protect: a shift by one (or
    a drop) still hands back a full set of well-formed results, so only the
    per-index pairing catches it.
    """
    assert set(spill.writes) == set(range(len(jobs)))
    for index, result in spill.writes.items():
        expected = jobs[index].normalized_url
        assert result.normalized_url == expected, (
            f"spill[{index}] holds {result.normalized_url!r}, expected {expected!r}"
        )


def _tiered_job(index: int, host: str) -> CrawlTask:
    url = f"http://{host}/"
    return CrawlTask(index=index, original_url=url, normalized_url=url, domain=host)


def _tiered_worker(fail_index: int | None = None):
    """Build an aligned result worker with an optional single failure index."""

    async def worker(job):
        if fail_index is not None and job.index == fail_index:
            raise RuntimeError("transient failure")
        return make_engine_result(job.normalized_url)

    return worker


def _open_scheduler(*pinned_domains: str):
    """A scheduler wide enough not to serialize, with ``pinned_domains`` on the browser tier."""
    scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
    for domain in pinned_domains:
        scheduler.set_pinned_strategy(domain, BROWSER_HEADLESS)
    return scheduler


def _interleaved_jobs():
    """Four jobs whose index order alternates browser, http, browser, http.

    The interleaving is the point: ``_pop_admitted`` emits tasks HTTP-first (tier
    order) while ``DomainScheduler.run`` reports results in index order with
    failures filtered out, so a batch that is all one tier never exercises the
    mis-pairing.
    """
    return [_tiered_job(0, "b1"), _tiered_job(1, "h1"), _tiered_job(2, "b2"), _tiered_job(3, "h2")]


async def test_mixed_http_browser_batch_aligns_each_result_to_its_own_index():
    """A batch that interleaves HTTP and browser-pinned tasks must still write
    each result to its own job index."""
    jobs = _interleaved_jobs()
    spill, _, _ = await _run(jobs, _tiered_worker(), scheduler=_open_scheduler("b1", "b2"))

    assert_spill_keeps_job_pairing(spill, jobs)


async def test_failed_job_does_not_shift_other_results():
    """A job whose worker raises must not shift the pairing of later jobs, and
    must still receive its own failure envelope instead of vanishing."""
    jobs = [_tiered_job(i, f"h{i}") for i in range(4)]
    spill, _, _ = await _run(jobs, _tiered_worker(fail_index=1), scheduler=_open_scheduler())

    assert_spill_keeps_job_pairing(spill, jobs)
    assert spill.writes[1].status == "failed"


async def test_pool_exhaustion_requeue_does_not_consume_a_max_pages_slot():
    """A re-queued transiently-failed URL was never crawled, so it must not cost a
    page against ``max_pages``. Before the fix each re-queue incremented
    ``page_count`` and a burst of exhaustion events could terminate the crawl
    before its real page budget was spent.

    The seed URL exhausts the pool on every attempt and is re-queued up to the
    cap; it also discovers one real page, which renders successfully. The
    budget admits exactly that real page after all the retries, so it is
    reached only when the retries themselves cost nothing.
    """
    from app.crawl.recursive import MAX_TRANSIENT_REQUEUES

    total_attempts = 1 + MAX_TRANSIENT_REQUEUES

    async def worker(job):
        if job.normalized_url == "http://a/seed":
            return engine_result(
                job.normalized_url,
                "browser_pool_exhausted",
                error="browser_pool_exhausted",
                # Links from an eternally-exhausted URL are expanded only when
                # its result is recorded, so the real page rides on the seed's
                # final permanent failure, not on any re-queue.
                discovered_links=[_link("http://a/real")],
            )
        return make_engine_result(job.normalized_url)

    jobs = [
        CrawlTask(
            index=0,
            original_url="http://a/seed",
            normalized_url="http://a/seed",
            domain="a",
        )
    ]

    spill, _scheduler, _gate = await _run(
        jobs,
        worker,
        max_depth=2,
        # Budget admits the seed's final permanent failure plus exactly one real
        # page and nothing spare: each phantom re-queue increment consumes a
        # slot, so under the bug the loop exits before /real is ever popped.
        max_pages=total_attempts,
    )

    statuses = {r.normalized_url: r.status for r in spill.writes.values()}
    # The real page must have been crawled despite the retry budget: it is
    # discovered only when the seed's permanent failure is finally recorded,
    # which happens at the tail of the loop.
    assert statuses.get("http://a/real") == "success", statuses
    assert statuses.get("http://a/seed") == "failed", statuses
