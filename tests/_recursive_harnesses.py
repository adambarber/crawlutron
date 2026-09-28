"""Harnesses for driving ``run_recursive_crawl`` from a test.

``run_recursive_crawl`` takes twenty keyword arguments. A test of the dispatch
loop is about at most four of them -- the jobs, the worker, whether robots
allows, and the bound under test -- and the other sixteen were spelled out in
full at every call site. That is a funnel that did not exist: a new required
keyword in production became a four-file edit, and a call site that quietly
kept a stale default passed for the wrong reason rather than failing.

``drive_recursive_crawl`` is that funnel. It takes the uncontested defaults from
``_crawl_defaults`` and binds every keyword against ``run_recursive_crawl``'s own
signature, so the harness cannot restate a contract and drift from it: the list
of keywords exists in exactly one place, production's, and a required keyword
added there becomes a ``TypeError`` at the harness rather than a four-file edit.
The defaults are the ones that mean "this test is not about admission, robots,
or document bodies":

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
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, NamedTuple

from app.crawl.recursive import run_recursive_crawl
from app.models import CrawlTask
from tests._policy_factories import UNRESTRICTED_HOST_POLICY
from tests._result_builders import Spill, make_engine_result
from tests._scheduler_doubles import FakeScheduler

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
    host_policy: Any | None = None,
    **overrides: Any,
) -> RecursiveRun:
    """Run one recursive crawl with the uncontested defaults filled in.

    The rest of ``run_recursive_crawl``'s keywords come from
    ``_crawl_defaults`` and are bound against ``run_recursive_crawl``'s own
    signature before the call, so this is the production contract rather than a
    second opinion about it -- derived, not restated. A new required keyword in
    production raises at the harness, once, instead of at one of several call
    sites, and the list of keywords exists in exactly one place: production's.

    ``overrides`` is not the ``**kwargs`` catch-all it looks like. Every value in
    it is bound against ``_CRAWL_SIGNATURE``, so a typo'd or misspelled override
    is a ``TypeError`` here, naming the parameter, rather than an argument
    production never received.
    """
    the_spill = spill if spill is not None else Spill()
    the_gate = gate if gate is not None else ContentionFreeGate(gate_scale)
    the_scheduler = scheduler if scheduler is not None else FakeScheduler()
    bound = _CRAWL_SIGNATURE.bind(
        **{
            "jobs": jobs,
            "worker": worker,
            "robots_allows": allows,
            "robots_crawl_delay": crawl_delay,
            "spill": the_spill,
            "scheduler": the_scheduler,
            "resource_gate": the_gate,
            "admission": admission,
            "host_policy": (host_policy if host_policy is not None else UNRESTRICTED_HOST_POLICY),
            **_crawl_defaults(robots_disallowed=robots_disallowed, sitemap_seeds=sitemap_seeds),
            **overrides,
        }
    )
    bound.apply_defaults()
    await run_recursive_crawl(**bound.arguments)
    return RecursiveRun(spill=the_spill, scheduler=the_scheduler, gate=the_gate)


#: ``run_recursive_crawl``'s own signature, read once at import. Every keyword the
#: harness forwards is bound against this, so the harness cannot drift from the
#: contract it exists to funnel -- there is no second list to forget to update.
_CRAWL_SIGNATURE = inspect.signature(run_recursive_crawl)


def _crawl_defaults(
    *, robots_disallowed: Callable[[CrawlTask], Any] | None, sitemap_seeds: Any
) -> dict[str, Any]:
    """The values for the keywords a crawl test is not about.

    Factories, not values: ``shutdown_event`` has to be a fresh ``Event`` per
    call, because a shared one carries ``is_set()`` state between tests and a
    crawl that should have run would stop immediately.

    A parameter absent here is required by the signature, which is the point --
    the harness is the contract, and a new required keyword in production becomes
    a ``TypeError`` here rather than a silent default nobody chose.
    """
    return {
        "robots_disallowed": (
            robots_disallowed
            if robots_disallowed is not None
            else (lambda job: make_engine_result(job.normalized_url))
        ),
        "sitemap_seeds": sitemap_seeds if sitemap_seeds is not None else _no_seeds,
        "shutdown_event": asyncio.Event(),
        "max_depth": 0,
        "max_pages": 50,
        "respect_robots": True,
        "include_html": True,
        "want_document_bodies": False,
        "scope_pattern": None,
        "global_concurrency": 4,
        "seed_sitemaps": False,
    }


async def _no_seeds() -> list[Any]:
    """No sitemap seeds. Yields once, because the closures a test passes do."""
    await asyncio.sleep(0)
    return []
