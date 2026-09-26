"""Harnesses for driving ``run_recursive_crawl`` from a test.

``run_recursive_crawl`` takes twenty keyword arguments. A test of the dispatch
loop is about at most four of them -- the jobs, the worker, whether robots
allows, and the bound under test -- and the other sixteen were spelled out in
full at every call site. That is a funnel that did not exist: a new required
keyword in production became a four-file edit, and a call site that quietly
kept a stale default passed for the wrong reason rather than failing.

``drive_recursive_crawl`` is that funnel. It is the production signature with
the uncontested defaults filled in, so a test says what it is about by naming
it and a contract change lands in one place. The defaults are the ones that
mean "this test is not about admission, robots, or document bodies":

* ``FakeScheduler`` runs the jobs it is handed, in order, with no domain
  politeness and no strategy pinning -- so a scheduling assertion is a test
  that passes its own scheduler in.
* ``ContentionFreeGate`` never contends. The real ``ResourceGate`` samples
  ``/sys/fs/cgroup`` and the wall clock, which would make a dispatch test's
  result depend on the machine it ran on; a double that reports full speed and
  zero usage is the hermetic way to say admission is not the subject.
* ``respect_robots=True`` with an ``allows`` closure the test supplies, and a
  ``robots_disallowed`` skip that records the URL as rendered. A test that is
  about robots passes a different ``allows``; a test that is about the
  skip path passes its own ``robots_disallowed``.
* ``spill`` and the frontier bookkeeping are the real ``Spill``, so what a test
  asserts on is what the loop actually wrote.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from _policy_factories import UNRESTRICTED_HOST_POLICY
from _result_builders import Spill, make_engine_result
from _scheduler_doubles import FakeScheduler

from app.crawl.recursive import run_recursive_crawl
from app.models import CrawlTask

__all__ = ["ContentionFreeGate", "RecursiveRun", "drive_recursive_crawl", "make_crawl_task"]


class ContentionFreeGate:
    """A ``ResourceGate`` that reports a fixed scale and no memory or CPU use.

    Stands in for ``app.resource_pressure.ResourceGate``, which reads the real
    cgroup and the real clock. What a dispatch test needs from a gate is a
    *known* scale, and that is a statement about the test rather than a
    reimplementation of the gate: a double that computed anything would be
    testing the double. ``scale=0.0`` is the interesting non-default, because it
    forces each batch down to the floor of one job and is how a test pins that
    shrinking the batch does not shrink the crawl.

    ``consultations`` counts the ``batch_scale_factor`` calls, which is the only
    gate method the recursive path actually invokes
    (``app/crawl/recursive_admission.py``). The gate also exposes
    ``wait_while_saturated``, and deliberately does not count it: the recursive
    loop must not consult it -- admission is per-batch weighted, not a
    stop-the-world pause -- so a counter on it is a counter nothing increments.
    """

    def __init__(self, scale: float = 1.0) -> None:
        self.scale = scale
        self.consultations = 0

    def batch_scale_factor(self) -> float:
        self.consultations += 1
        return self.scale

    def memory_ratio(self) -> float:
        return 0.0

    def cpu_ratio(self) -> float:
        return 0.0

    async def wait_while_saturated(self, max_wait_seconds: float | None = None) -> None:
        """Present because ``ResourceGate`` has it, never called by the recursive path."""
        raise AssertionError("the recursive loop must not stop the world for admission")


class RecursiveRun(NamedTuple):
    """What one ``drive_recursive_crawl`` call left behind."""

    spill: Spill
    scheduler: FakeScheduler
    gate: ContentionFreeGate


def make_crawl_task(index: int, host: str) -> CrawlTask:
    """A seed task for ``http://<host>/page<index>``."""
    url = f"http://{host}/page{index}"
    return CrawlTask(index=index, original_url=url, normalized_url=url, domain=host)


async def drive_recursive_crawl(
    jobs: list[CrawlTask],
    worker: Callable[[CrawlTask], Awaitable[Any]],
    *,
    allows: Callable[[str], Awaitable[bool]],
    crawl_delay: Callable[[str], Awaitable[float | None]] | None = None,
    robots_disallowed: Callable[[CrawlTask], Any] | None = None,
    sitemap_seeds: Callable[[], Awaitable[list[Any]]] | None = None,
    spill: Spill | None = None,
    scheduler: FakeScheduler | None = None,
    gate: ContentionFreeGate | None = None,
    gate_scale: float = 1.0,
    admission: Any | None = None,
    shutdown_event: asyncio.Event | None = None,
    host_policy: Any | None = None,
    max_depth: int = 0,
    max_pages: int = 50,
    respect_robots: bool = True,
    include_html: bool = True,
    want_document_bodies: bool = False,
    scope_pattern: Any | None = None,
    global_concurrency: int = 4,
    seed_sitemaps: bool = False,
) -> RecursiveRun:
    """Run one recursive crawl with the uncontested defaults filled in.

    Every keyword here mirrors ``run_recursive_crawl``'s, default-for-default,
    so the harness is the production contract and not a second opinion about it:
    a new required argument in production raises here, once, instead of being
    missed at one of several call sites. There is no ``**kwargs`` catch-all on
    purpose -- a typo'd or misspelled override must be a ``TypeError`` at the
    harness rather than an argument production never received.
    """
    the_spill = spill if spill is not None else Spill()
    the_gate = gate if gate is not None else ContentionFreeGate(gate_scale)
    the_scheduler = scheduler if scheduler is not None else FakeScheduler()
    await run_recursive_crawl(
        jobs=jobs,
        scheduler=the_scheduler,
        worker=worker,
        robots_allows=allows,
        robots_crawl_delay=crawl_delay,
        robots_disallowed=(
            robots_disallowed
            if robots_disallowed is not None
            else (lambda job: make_engine_result(job.normalized_url))
        ),
        sitemap_seeds=sitemap_seeds if sitemap_seeds is not None else _no_seeds,
        spill=the_spill,
        shutdown_event=shutdown_event if shutdown_event is not None else asyncio.Event(),
        max_depth=max_depth,
        max_pages=max_pages,
        respect_robots=respect_robots,
        include_html=include_html,
        want_document_bodies=want_document_bodies,
        scope_pattern=scope_pattern,
        global_concurrency=global_concurrency,
        seed_sitemaps=seed_sitemaps,
        resource_gate=the_gate,
        admission=admission,
        host_policy=host_policy if host_policy is not None else UNRESTRICTED_HOST_POLICY,
    )
    return RecursiveRun(spill=the_spill, scheduler=the_scheduler, gate=the_gate)


async def _no_seeds() -> list[Any]:
    """No sitemap seeds. Yields once, because the closures a test passes do."""
    await asyncio.sleep(0)
    return []
