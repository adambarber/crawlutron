"""Direct unit tests for the extracted recursive dispatch (app.orchestration.recursive).

These drive :func:`run_recursive_crawl` with fake closures/scheduler/spill so
the batch-sizing and admission-gate logic is covered without standing up the
full engine (the reason the dispatch was extracted from ``crawl_request``).
"""

from urllib.parse import urlsplit

import pytest

from app.models.crawl_task import CrawlTask
from app.models.discovered_link import DiscoveredLink
from app.orchestration.scheduler import DomainScheduler
from app.vocabulary.strategies import BROWSER_HEADLESS
from tests._policy_factories import canonical
from tests._recursive_harnesses import ContentionFreeGate, drive_recursive_crawl, make_crawl_task
from tests._result_builders import engine_result, make_engine_result

# The root seed as written, and the value the engine actually holds for it. These are not the same string:
# normalization strips that trailing slash. Naming both makes the distinction something a test can see.
_ROOT = "http://a/"
_ROOT_CANONICAL = canonical(_ROOT)


def _task(url: str, *, index: int = 0) -> CrawlTask:
    """A crawl task whose ``normalized_url`` is the production canonical form.

    ``original_url`` keeps the raw spelling, so the pair differs exactly where
    normalization changed something. The four-field seed was written out six
    times in this file, each copy free to drift from the canonical form on its
    own; this is the one place that cannot.
    """
    return CrawlTask(
        index=index,
        original_url=url,
        normalized_url=canonical(url),
        domain=urlsplit(url).hostname or "",
    )


def _link(url: str) -> DiscoveredLink:
    return DiscoveredLink(
        url=url,
        normalized_url=canonical(url),
        text="",
        rel=[],
        source_url=canonical("http://a/"),
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


def _expanding_worker():
    """A worker whose ROOT result discovers ``/1`` and ``/2``; every other page is a leaf.

    One definition of the site these dispatch tests crawl, so a test that differs
    only in the bound under test does not restate the worker. That matters here
    specifically because the ``max_pages`` pair asserts the unbounded reading and
    the bounded one against the SAME crawl -- if each spelled its own worker, the
    two could expand to different shapes and stop being comparable, while still
    passing individually.

    Only the root carries links, so the frontier terminates at exactly three
    pages and the page count IS the shape under test.
    """

    async def worker(job):
        links = (
            [_link("http://a/1"), _link("http://a/2")]
            if job.normalized_url == canonical(_ROOT_CANONICAL)
            else []
        )
        return make_engine_result(job.normalized_url, links)

    return worker


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

    jobs = [_task(_ROOT)]
    spill, scheduler, gate = await _run(jobs, worker)
    assert len(spill.writes) == 1
    assert scheduler.runs == 1
    # Admission is per-batch weighted, not a stop-the-world pause: the gate is consulted for a scale and the
    # batch is dispatched anyway.
    assert gate.consultations >= 1
    assert gate.scale == 1.0


async def test_expands_frontier_from_discovered_links():
    def _links_for(url):
        return [_link("http://a/1"), _link("http://a/2")] if url == canonical(_ROOT_CANONICAL) else []

    async def worker(job):
        return make_engine_result(job.normalized_url, _links_for(job.normalized_url))

    jobs = [_task(_ROOT)]
    spill, scheduler, _ = await _run(jobs, worker)
    # root + /1 + /2
    assert len(spill.writes) == 3
    assert set(spill.writes[i].normalized_url for i in sorted(spill.writes)) == {
        canonical(_ROOT_CANONICAL),
        canonical("http://a/1"),
        canonical("http://a/2"),
    }


async def test_gate_scale_shrinks_batch_but_still_crawls_all():
    # scale 0.0 forces batch_size to its floor (>=1); every discovered page must still be crawled, just across
    # more scheduler runs.
    spill, scheduler, _ = await _run([_task(_ROOT)], _expanding_worker(), gate=ContentionFreeGate(0.0))
    assert len(spill.writes) == 3
    assert scheduler.runs >= 2  # shrunk batches => multiple scheduler runs


async def test_max_pages_zero_is_unbounded_at_the_dispatch_loop():
    """A cap of 0 means "no cap" here, so it must not fetch nothing.

    ``remaining_page_capacity`` has always answered None -- unbounded -- for
    ``max_pages <= 0``, and a whole-site job read back from its row keeps its
    stored 0 all the way here: ``validate_wire_policy`` returns an already-built
    model as it stands, so the API edge's "a crawl must be bounded" substitution
    does not apply to it. That makes this the only place 0 reaches the loop, and
    the loop compared ``page_count < max_pages`` directly -- so it read the
    unbounded sentinel as "already at the cap" and returned before the first
    batch. A whole-site job asking for an unlimited crawl returned zero results,
    and because the loop had done nothing wrong there was no error to explain it.

    Driven directly rather than through the API precisely because the API path
    substitutes a budget and would have hidden this: a test through
    ``crawl_request`` passes here today, for a reason unrelated to the loop.
    """

    spill, _scheduler, _gate = await _run([_task(_ROOT)], _expanding_worker(), max_pages=0)
    assert set(spill.writes) == {0, 1, 2}, (
        f"max_pages=0 wrote {sorted(spill.writes)}; 0 means unbounded, so root + /1 + /2"
    )


async def test_a_positive_cap_still_bounds_the_same_crawl():
    """The unbounded reading must not have cost the bounded one.

    Stated against the same site and the same harness as the test above, because
    "treat 0 as unbounded" and "ignore the cap" are the same edit to a reader
    skimming a diff, and only a positive cap in the same fixture separates them.
    """

    spill, _scheduler, _gate = await _run([_task(_ROOT)], _expanding_worker(), max_pages=1)
    assert set(spill.writes) == {0}, f"a cap of 1 admitted {sorted(spill.writes)} pages"


async def test_robots_disallows_discovered_link_silently():
    # A discovered link that robots disallows is neither crawled nor written.
    seen = []

    async def worker(job):
        seen.append(job.normalized_url)
        links = [_link("http://a/blocked")] if job.normalized_url == canonical(_ROOT_CANONICAL) else []
        return make_engine_result(job.normalized_url, links)

    async def allows(url):
        return url.raw != "http://a/blocked"

    jobs = [_task(_ROOT)]
    spill, _, _ = await _run(jobs, worker, robots_allows=allows, respect_robots=True)
    assert seen == [canonical(_ROOT_CANONICAL)]  # the blocked link was never crawled
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {canonical(_ROOT_CANONICAL)}


async def test_robots_disallows_initial_job_as_skip(recording_worker):
    # An initial job that robots disallows is written as a skip, never crawled.
    seen, worker = recording_worker

    async def allows(url):
        return url.raw != "http://a/blocked"

    jobs = [_task("http://a/blocked")]
    spill, _, _ = await _run(jobs, worker, robots_allows=allows, respect_robots=True)
    assert seen == []  # worker never invoked
    assert set(spill.writes[i].normalized_url for i in spill.writes) == {canonical("http://a/blocked")}


# --------------------------------------------------------------------------- Result alignment: the spill
# index must match the job that produced the result, even when tiers interleave or a job fails.
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
    return CrawlTask(
        index=index,
        original_url=f"http://{host}/",
        normalized_url=canonical(f"http://{host}/"),
        domain=host,
    )


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


async def test_pool_exhaustion_requeue_does_not_consume_a_max_pages_slot(): ...


async def test_in_flight_pages_count_against_the_cap_at_expansion():
    """Pages admitted this batch but not yet committed are OCCUPIED pages.

    Two seeds share one in-flight batch, so when the first commit expands its
    discovered links the second seed is admitted-but-uncommitted. The page
    budget has three slots and two are spent on seeds, so exactly one slot is
    left for discovered pages. The hand-passed
    ``occupied=page_count+len(frontier)`` read 1+0 here -- both in-flight
    seeds invisible -- so expansion robots-queried a page the cap already
    refused and queued a phantom past the last slot. Occupation is now
    DERIVED from the module's own reserved set (known ∪ frontier identities),
    which is exactly the pages a crawl holds: nothing to hand-compute,
    nothing to get wrong.
    """
    probed: list[str] = []

    async def allows(url):
        probed.append(url.raw)
        return True

    async def worker(job):
        if job.normalized_url == canonical("http://a/0"):
            return make_engine_result(
                job.normalized_url,
                [_link(f"http://a/{i}") for i in (1, 2, 3)],
            )
        return make_engine_result(job.normalized_url)

    spill, _scheduler, _gate = await drive_recursive_crawl(
        [_task("http://a/0"), _task("http://a/x", index=1)],
        worker,
        allows=allows,
        max_depth=1,
        max_pages=3,
        respect_robots=True,
    )

    # Page 3 is the falsifier: the write cap holds under the old reading too (the dispatch loop rechecks it
    # per batch), so the breach shows one level earlier -- a page the cap refuses must never reach robots.
    assert "http://a/3" not in probed
    assert {r.normalized_url for r in spill.writes.values()} == {
        canonical("http://a/0"),
        canonical("http://a/x"),
        canonical("http://a/1"),
    }
    """A re-queued transiently-failed URL was never crawled, so it must not cost a
    page against ``max_pages``. Before the fix each re-queue incremented
    ``page_count`` and a burst of exhaustion events could terminate the crawl
    before its real page budget was spent.

    The seed URL exhausts the pool on every attempt and is re-queued up to the
    cap; it also discovers one real page, which renders successfully. The
    budget admits exactly that real page after all the retries, so it is
    reached only when the retries themselves cost nothing.
    """
    from app.orchestration.recursive import MAX_TRANSIENT_REQUEUES

    total_attempts = 1 + MAX_TRANSIENT_REQUEUES

    async def worker(job):
        if job.normalized_url == canonical("http://a/seed"):
            return engine_result(
                job.normalized_url,
                "browser_pool_exhausted",
                error="browser_pool_exhausted",
                # Links from an eternally-exhausted URL are expanded only when its result is recorded, so the
                # real page rides on the seed's final permanent failure, not on any re-queue.
                discovered_links=[_link("http://a/real")],
            )
        return make_engine_result(job.normalized_url)

    jobs = [_task("http://a/seed")]

    spill, _scheduler, _gate = await _run(
        jobs,
        worker,
        max_depth=2,
        max_pages=total_attempts,
    )

    statuses = {r.normalized_url: r.status for r in spill.writes.values()}
    # The real page must have been crawled despite the retry budget: it is discovered only when the seed's
    # permanent failure is finally recorded, which happens at the tail of the loop.
    assert statuses.get(canonical("http://a/real")) == "success", statuses
    assert statuses.get(canonical("http://a/seed")) == "failed", statuses
