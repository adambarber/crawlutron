"""Recursive-crawl admission/task doubles shared across orchestration tests.

Declared support surface (#476): moved verbatim from
test_recursive_dispatch.py so a sibling test module imports these from here,
not from another test's internals.
"""

from __future__ import annotations

from collections import deque

from app.models.crawl_task import CrawlTask
from app.models.discovered_link import DiscoveredLink
from app.models.host_policy import HostPolicy
from app.orchestration.recursive import RecursiveRunState
from app.transport.scheduler import DomainScheduler
from app.vocabulary.strategies import BROWSER_HEADLESS
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


def minimum_admission_run(frontier, *, http_quota, browser_quota, batch_pages=10, browser_domains=()):
    """A minimal run/state for ``admit_recursive_batch``: only the admission
    path is exercised, with ``respect_robots=False`` short-circuiting robots.
    """
    from app.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER

    class FixedAdmission:
        def __init__(self, h, b):
            self.h, self.b = h, b
            self.last_want = None

        async def admit(self, want_http, want_browser):
            self.last_want = (want_http, want_browser)
            return min(want_http, self.h), min(want_browser, self.b)

    admission = FixedAdmission(http_quota, browser_quota)
    scheduler = DomainScheduler(global_limit=8, per_domain_limit=8, delay_seconds=0.0)
    for domain in browser_domains:
        scheduler.set_pinned_strategy(domain, BROWSER_HEADLESS)
    run_state = RecursiveRunState(
        scheduler=scheduler,
        worker=None,  # type: ignore[arg-type]  # admit_recursive_batch never dispatches on it
        robots_allows=None,  # type: ignore[arg-type]  # respect_robots=False short-circuits
        robots_crawl_delay=None,
        robots_disallowed=None,  # type: ignore[arg-type]  # respect_robots=False short-circuits
        sitemap_seeds=None,  # type: ignore[arg-type]  # seed_sitemaps=False
        spill=None,
        max_depth=0,
        max_pages=batch_pages,
        respect_robots=False,
        include_html=True,
        want_document_bodies=False,
        scope_pattern=None,
        global_concurrency=4,
        seed_sitemaps=False,
        resource_gate=ContentionFreeGate(1.0),  # type: ignore[arg-type]  # structurally satisfied
        admission=admission,  # type: ignore[arg-type]  # FixedAdmission is the admit() shape
        host_policy=HostPolicy.unrestricted(),
        ramp_max=128,
        http_heavy_lookahead=128 * ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
        spill_write=None,  # type: ignore[arg-type]  # spill is None
    )
    state = type(
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
    return run_state, state, admission
