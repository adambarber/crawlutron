"""Harnesses for driving ``run_recursive_crawl`` from a test: a dispatch test is
about at most four of twenty keywords, so ``drive_recursive_crawl`` binds the
uncontested defaults against production's own signature rather than restating it.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, NamedTuple

from safe_url.normalized_url import NormalizedUrl

from crawlutron.models.crawl_task import CrawlTask
from crawlutron.orchestration.recursive import run_recursive_crawl
from tests._admission_doubles import FakeResourceGauge
from tests._policy_factories import UNRESTRICTED_HOST_POLICY, canonical
from tests._result_builders import Spill, engine_result
from tests._scheduler_doubles import FakeScheduler

if TYPE_CHECKING:
    # The types the harness only names, not the ones it builds: kept out of the
    # runtime imports so this file does not restate the module's own import block.
    from safe_url.host_policy import HostPolicy

    from crawlutron.models.crawl_result import EngineCrawlResult
    from crawlutron.runtime.pressure_sizing import AdmissionGate
    from crawlutron.transport.scheduler import DomainScheduler

__all__ = [
    "ContentionFreeGate",
    "RecursiveRun",
    "_allowed_url",
    "drive_recursive_crawl",
    "make_crawl_task",
]


class ContentionFreeGate(FakeResourceGauge):
    """A FakeResourceGauge that reports a fixed scale and no memory or CPU use, so a
    dispatch test's result does not depend on the machine; ``scale=0.0`` forces a
    batch floor of one job, and ``consultations`` counts the one method it invokes.
    """

    def __init__(self, scale: float = 1.0) -> None:
        super().__init__()
        self.scale = scale
        self.consultations = 0

    def batch_scale_factor(self) -> float:
        self.consultations += 1
        return self.scale

    def memory_ratio(self) -> float:
        return 0.0

    def cpu_ratio(self) -> float:
        return 0.0


class RecursiveRun(NamedTuple):
    """What one ``drive_recursive_crawl`` call left behind."""

    spill: Spill
    scheduler: FakeScheduler | DomainScheduler | None
    gate: ContentionFreeGate


def make_crawl_task(index: int, host: str) -> CrawlTask:
    """A seed task for ``http://<host>/page<index>``."""
    url = f"http://{host}/page{index}"
    return CrawlTask(index=index, original_url=url, normalized_url=canonical(url))


async def drive_recursive_crawl(
    jobs: list[CrawlTask],
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]],
    *,
    allows: Callable[[NormalizedUrl], Awaitable[bool]],
    crawl_delay: Callable[[NormalizedUrl], Awaitable[float | None]] | None = None,
    robots_disallowed: Callable[[CrawlTask], Awaitable[EngineCrawlResult]] | None = None,
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]] | None = None,
    spill: Spill | None = None,
    scheduler: FakeScheduler | DomainScheduler | None = None,
    gate: ContentionFreeGate | None = None,
    gate_scale: float = 1.0,
    admission: AdmissionGate | None = None,
    host_policy: HostPolicy | None = None,
    **overrides: object,
) -> RecursiveRun:
    """Run one recursive crawl with the uncontested defaults filled in; every keyword
    is bound against ``run_recursive_crawl``'s own signature first, so this is derived
    rather than restated, and a typo'd ``overrides`` value is a ``TypeError`` here.
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
            "request_id": "r-recursive-probe",
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


#: ``run_recursive_crawl``'s own signature, read once at import.
_CRAWL_SIGNATURE = inspect.signature(run_recursive_crawl)


def _crawl_defaults(
    *,
    robots_disallowed: Callable[[CrawlTask], Awaitable[EngineCrawlResult]] | None,
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]] | None,
) -> dict[str, object]:
    """The values for the keywords a crawl test is not about: factories, not values,
    so ``shutdown_event`` is a fresh ``Event`` per call -- a shared one carries
    ``is_set()`` state between tests and would stop a crawl that should have run.
    """
    return {
        "robots_disallowed": (robots_disallowed if robots_disallowed is not None else _allowed_result),
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


async def _allowed_url(url: NormalizedUrl) -> bool:
    """The allow-all robots answer, as RobotsAllows declares it."""
    return True


async def _allowed_result(job: CrawlTask) -> EngineCrawlResult:
    """The result for a URL a crawl test did not expect to be denied."""
    return engine_result(job.normalized_url.raw, "rendered")


async def _no_seeds() -> list[NormalizedUrl]:
    """No sitemap seeds. Yields once, because the closures a test passes do."""
    await asyncio.sleep(0)
    return []
