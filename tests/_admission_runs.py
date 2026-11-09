"""Recursive-crawl admission/task doubles shared across orchestration tests.

Declared support surface (#476): moved verbatim from
test_recursive_dispatch.py so a sibling test module imports these from here,
not from another test's internals.
"""

from __future__ import annotations

from collections import deque
from typing import cast

from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.crawl_task import CrawlTask
from crawlutron.models.discovered_link import DiscoveredLink
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.orchestration.recursive import CrawlRun
from crawlutron.orchestration.spill_sink import SpillSink
from crawlutron.runtime.pressure_gates import ResourceGate
from crawlutron.runtime.pressure_sizing import WeightedAdmission
from crawlutron.transport.scheduler import DomainScheduler
from crawlutron.vocabulary.strategies import BROWSER_HEADLESS
from tests._policy_factories import canonical
from tests._recursive_harnesses import ContentionFreeGate

__all__ = ["link", "minimum_admission_run", "run", "task"]


async def _allows(url):
    return True


async def run(
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

    The bound under test is passed explicitly, so the depth default is the
    only reason this wrapper exists."""
    from tests._recursive_harnesses import drive_recursive_crawl

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


def task(url: str, *, index: int = 0) -> CrawlTask:
    """A crawl task whose ``normalized_url`` is the production canonical form."""
    return CrawlTask(
        index=index,
        original_url=url,
        normalized_url=canonical(url),
    )


def link(url: str) -> DiscoveredLink:
    return DiscoveredLink(
        url=url,
        normalized_url=canonical(url),
        text="",
        rel=[],
        source_url=canonical("http://a/"),
        host="a",
        is_same_host=True,
    )


class FixedAdmission:
    """The one admission behavior under test: caps per tier, remembers what it saw."""

    def __init__(self, h, b):
        self.h, self.b = h, b
        self.last_want = None

    async def admit(self, want_http, want_browser):
        self.last_want = (want_http, want_browser)
        return min(want_http, self.h), min(want_browser, self.b)


# The admission path must never dispatch on the crawl-carrying fields; each stub
# is a typed no-op that FAILS LOUDLY if the path ever touches it.
async def _must_not_worker(task: CrawlTask) -> EngineCrawlResult:
    raise AssertionError("the admission path dispatched on worker")


async def _must_not_robots_allows(url: NormalizedUrl) -> bool:
    raise AssertionError("the admission path dispatched on robots_allows")


async def _must_not_robots_disallowed(task: CrawlTask) -> EngineCrawlResult:
    raise AssertionError("the admission path dispatched on robots_disallowed")


async def _must_not_sitemap_seeds() -> list[NormalizedUrl]:
    raise AssertionError("the admission path dispatched on sitemap_seeds")


async def _must_not_spill_write(sink: SpillSink | None, index: int, result: EngineCrawlResult) -> None:
    raise AssertionError("the admission path wrote a spill")


def _minimum_admission_state(frontier, *, batch_pages: int):
    """The batch state shape the dispatch loop's ``_take_and_bucket`` reads."""
    return type(
        "State",
        (),
        {
            # The frontier carries ``(depth, task)`` pairs, like the dispatch loop
            # builds; ``_take_and_bucket`` subscriptable-addresses ``item[1]``.
            "frontier": deque((0, item) for item in frontier),
            "visited": set(),
            "ramp": 4.0,
            "page_count": 0,
        },
    )()


def minimum_admission_run(frontier, *, http_quota, browser_quota, batch_pages=10, browser_domains=()):
    """A minimal run/state for ``admit_recursive_batch``: only the admission
    path is exercised, with ``respect_robots=False`` short-circuiting robots.
    """
    from crawlutron.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER

    admission = FixedAdmission(http_quota, browser_quota)
    scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
    for domain in browser_domains:
        scheduler.set_pinned_strategy(domain, BROWSER_HEADLESS)

    # FixedAdmission and ContentionFreeGate satisfy the two admission fields
    # structurally; the casts are the provenance-documented-double pattern.
    run_state = CrawlRun(
        scheduler=scheduler,
        worker=_must_not_worker,
        robots_allows=_must_not_robots_allows,
        robots_crawl_delay=None,
        robots_disallowed=_must_not_robots_disallowed,
        sitemap_seeds=_must_not_sitemap_seeds,
        spill=None,
        max_depth=0,
        max_pages=batch_pages,
        respect_robots=False,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=4,
        seed_sitemaps=False,
        resource_gate=cast("ResourceGate", ContentionFreeGate(1.0)),  # provenance: structurally satisfied
        admission=cast("WeightedAdmission", admission),  # provenance: FixedAdmission is the admit() shape
        host_policy=HostPolicy.unrestricted(),
        ramp_max=128,
        http_heavy_lookahead=128 * ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
        spill_write=_must_not_spill_write,
    )
    return run_state, _minimum_admission_state(frontier, batch_pages=batch_pages), admission
