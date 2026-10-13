"""Resource and robots admission for one recursive crawl batch."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any, NamedTuple, Protocol

from app.admission.admission import gather_robots_decisions
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.scheduler import DomainScheduler
from app.runtime.metrics import MetricsStore
from app.runtime.resource_pressure import ResourceGate, WeightedAdmission
from app.safety.safe_regex import BoundedPattern
from app.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER
from app.vocabulary.strategies import BROWSER_STRATEGIES, HTTP_STRATEGIES

logger = logging.getLogger(__name__)

# One admitted item as it flows down the pipeline: (depth, task), named once so every
# stage boundary states the shape by reference instead of re-spelling it.
type _Admitted = tuple[int, CrawlTask]


class _BatchState(Protocol):
    frontier: deque[tuple[int, CrawlTask]]
    visited: set[NormalizedUrl]
    ramp: float
    page_count: int


@dataclass
class RecursiveRunState:
    """Everything one recursive crawl carries while it runs.

    This lives here rather than beside the dispatch loop that assembles it
    because admission is the consumer that defines what a run has to offer: the
    loop builds one of these and hands it down, and the stage signatures below
    read it. It was previously declared twice -- once as a concrete dataclass
    beside the loop, once as a ``_BatchRun`` protocol beside the stages -- and
    the two lists had to be kept in step by hand. One declaration, in the module
    that consumes it, is the version where they cannot drift.

    Fields the admission stages never read (``max_depth``, ``include_html``,
    ``want_document_bodies``, ``scope_pattern``, ``seed_sitemaps``) are still
    here: this is the run's state, and the dispatch stages further down the
    pipeline read the same object.
    """

    scheduler: DomainScheduler
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]]
    robots_allows: Callable[[NormalizedUrl], Awaitable[bool]]
    robots_crawl_delay: Callable[[NormalizedUrl], Awaitable[float | None]] | None
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult]
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]]
    spill: Any
    max_depth: int
    max_pages: int
    respect_robots: bool
    include_html: bool
    want_document_bodies: bool
    scope_pattern: BoundedPattern | None
    global_concurrency: int
    seed_sitemaps: bool
    resource_gate: ResourceGate
    admission: WeightedAdmission
    host_policy: HostPolicy
    ramp_max: int
    http_heavy_lookahead: int
    spill_write: Callable[[Any, int, EngineCrawlResult], Awaitable[None]]


class _BatchContext(NamedTuple):
    """Everything a batch-admission stage is allowed to see.

    The ``(run, state, metrics)`` triple is bundled because a stage must never be
    handed a ``state`` belonging to a different crawl: there is one context
    object flowing down the pipeline, built once by ``admit_recursive_batch``, so
    a stage cannot be wired to one run's gate and another run's bookkeeping.
    """

    run: RecursiveRunState
    state: _BatchState
    metrics: MetricsStore


class _PinProvider(Protocol):
    """The one scheduler method admission reads.

    ``DomainScheduler`` has always had ``get_pinned_strategy``, and the three
    admission decisions below were unified behind :func:`_pinned_strategy`
    precisely so the question was asked in one place. What was left over was the
    ``getattr`` guard that let a scheduler *without* the hook through -- an
    escape hatch for a shape the signature already forbade, and one that left
    every caller's result un-narrowed. Naming the surface rather than the class
    states the real requirement: anything that can answer the pin lookup, which
    is the real scheduler and the test double alike.
    """

    def get_pinned_strategy(self, domain: str) -> str | None: ...


def _pinned_strategy(scheduler: _PinProvider, task: CrawlTask) -> str | None:
    """The strategy this task's domain is pinned to, or None when unpinned.

    The one place the scheduler's per-domain pinning is read. Each of the
    three admission decisions below needs it and each spelled its own
    getattr-guard -- under two different local names, ``get_pin`` and
    ``get_pinned`` -- so a scheduler without the hook was handled three
    different ways, and a rename had to be caught three times.
    """
    return scheduler.get_pinned_strategy(task.domain)


def _batch_is_http_heavy(
    scheduler: _PinProvider,
    frontier: Sequence[tuple[int, CrawlTask]],
    sample: int,
) -> bool:
    if not frontier or sample <= 0:
        return False
    counted = 0
    http_count = 0
    for _, task in islice(frontier, sample):
        if task.domain is None:
            continue
        counted += 1
        if _pinned_strategy(scheduler, task) in HTTP_STRATEGIES:
            http_count += 1
    return counted > 0 and http_count >= counted / 2


def _count_strategy_tiers(
    scheduler: _PinProvider,
    frontier: Sequence[tuple[int, CrawlTask]],
    count: int,
) -> tuple[int, int]:
    http = 0
    browser = 0
    for _, task in islice(frontier, count):
        if _pinned_strategy(scheduler, task) in BROWSER_STRATEGIES:
            browser += 1
        else:
            http += 1
    return http, browser


def _pop_admitted(
    frontier: deque[tuple[int, CrawlTask]],
    admit_http: int,
    admit_browser: int,
    scheduler: _PinProvider,
) -> list[_Admitted]:
    if admit_http + admit_browser == 0:
        return []
    taken: list[_Admitted] = []
    while frontier and len(taken) < admit_http + admit_browser:
        taken.append(frontier.popleft())
    http_tasks: list[_Admitted] = []
    browser_tasks: list[_Admitted] = []
    for item in taken:
        _depth, task = item
        (browser_tasks if _pinned_strategy(scheduler, task) in BROWSER_STRATEGIES else http_tasks).append(
            item
        )
    selected = http_tasks[:admit_http] + browser_tasks[:admit_browser]
    excess = http_tasks[admit_http:] + browser_tasks[admit_browser:]
    for item in reversed(excess):
        frontier.appendleft(item)
    return selected


async def _resource_admitted_batch(ctx: _BatchContext) -> list[_Admitted]:
    run, state, metrics = ctx
    scale = run.resource_gate.batch_scale_factor()
    metrics.resource_memory_utilization = run.resource_gate.memory_ratio()
    metrics.resource_cpu_utilization = run.resource_gate.cpu_ratio()
    http_scale = getattr(run.resource_gate, "http_scale_factor", None)
    if http_scale is not None and _batch_is_http_heavy(
        run.scheduler, state.frontier, run.http_heavy_lookahead
    ):
        scale = http_scale()
    cpu = getattr(run.resource_gate, "cpu", None)
    concurrency_multiplier = ENGINE_BATCH_CONCURRENCY_MULTIPLIER
    if cpu is not None and not cpu.is_warm():
        concurrency_multiplier = 1
    if scale >= 1.0:
        state.ramp = min(float(run.ramp_max), state.ramp * 1.10)
    else:
        state.ramp = max(float(run.global_concurrency), state.ramp * scale)
    base = max(1, int(state.ramp))
    batch_size = min(
        len(state.frontier),
        max(1, base * concurrency_multiplier),
        max(1, run.max_pages - state.page_count),
    )
    if scale < 1.0:
        limited_by = "cpu" if run.resource_gate.cpu_ratio() >= run.resource_gate.memory_ratio() else "memory"
        logger.debug(
            "resource admission: throttling batch (scale=%.2f, limited_by=%s, mem=%.2f cpu=%.2f)",
            scale,
            limited_by,
            metrics.resource_memory_utilization,
            metrics.resource_cpu_utilization,
        )
        batch_size = max(1, int(batch_size * scale))
        metrics.record_throttle()
    want_http, want_browser = _count_strategy_tiers(run.scheduler, state.frontier, batch_size)
    admit_http, admit_browser = await run.admission.admit(want_http, want_browser)
    if admit_http + admit_browser < batch_size:
        metrics.record_throttle()
        batch_size = admit_http + admit_browser
    return _pop_admitted(state.frontier, admit_http, admit_browser, run.scheduler)


async def _robots_admitted_batch(ctx: _BatchContext, batch: list[_Admitted]) -> list[_Admitted]:
    run, state = ctx.run, ctx.state
    pending: list[_Admitted] = []
    seen_in_batch: set[NormalizedUrl] = set()
    for depth, job in batch:
        if job.normalized_url in state.visited or job.normalized_url in seen_in_batch:
            continue
        seen_in_batch.add(job.normalized_url)
        pending.append((depth, job))
    urls: list[NormalizedUrl] = [job.normalized_url for _, job in pending]
    decisions = (
        await gather_robots_decisions(urls, run.robots_allows, run.robots_crawl_delay)
        if run.respect_robots
        else [(True, None)] * len(pending)
    )
    allowed_jobs: list[_Admitted] = []
    for (depth, job), (allowed, delay) in zip(pending, decisions, strict=True):
        if run.respect_robots and not allowed:
            await run.spill_write(run.spill, job.index, run.robots_disallowed(job))
            state.visited.add(job.normalized_url)
            state.page_count += 1
            continue
        if run.respect_robots and delay and delay > 0:
            run.scheduler.set_domain_min_delay(job.domain, delay)
        allowed_jobs.append((depth, job))
        state.visited.add(job.normalized_url)
    return allowed_jobs


async def admit_recursive_batch(
    run: RecursiveRunState,
    state: _BatchState,
    metrics: MetricsStore,
) -> list[_Admitted]:
    """Resource-gate the frontier, then robots-filter whatever survived.

    The only place the batch-admission context is assembled. Both stages take
    the resulting ``_BatchContext``, so the stage protocol is stated once here
    instead of being re-spelled per stage.
    """
    ctx = _BatchContext(run, state, metrics)
    return await _robots_admitted_batch(ctx, await _resource_admitted_batch(ctx))


__all__ = ["admit_recursive_batch"]
