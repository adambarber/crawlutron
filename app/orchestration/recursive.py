"""Recursive crawl dispatch (bounded BFS) with self-tuning admission.
One home for the whole recursive walk: the resource-gated batch admission stages
and the dispatch loop that drives them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
from collections import deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from itertools import islice
from typing import Any, NamedTuple, Protocol, cast

from app.admission.admission_links import AdmittedLink, admit_discovered_links, gather_robots_decisions
from app.models.crawl_result import EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.results import failed_result_for
from app.orchestration.scheduler import DomainScheduler
from app.runtime.metrics import MetricsStore, get_metrics
from app.runtime.resource_pressure import (
    ResourceGate,
    WeightedAdmission,
    default_admission_policy,
)
from app.safety.safe_regex import BoundedPattern
from app.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER
from app.vocabulary.strategies import BROWSER_STRATEGIES, HTTP_STRATEGIES
from app.vocabulary.taxonomy import is_transient_classification

logger = logging.getLogger(__name__)

# A transiently-failed URL is requeued, not dead; the cap stops an unreachable host.
MAX_TRANSIENT_REQUEUES = 3

# One admitted item as it flows down the pipeline: (depth, task), named once so every
# stage boundary states the shape by reference instead of re-spelling it.
type _Admitted = tuple[int, CrawlTask]


#: The spill a run writes results to -- typed (#193) so a sink must accept ``(index, result)``.
class _SpillSink(Protocol):
    def write(self, index: int, result: EngineCrawlResult) -> Awaitable[None] | None: ...


class _BatchState(Protocol):
    frontier: deque[tuple[int, CrawlTask]]
    visited: set[NormalizedUrl]
    ramp: float
    page_count: int


@dataclass
class RecursiveRunState:
    """Everything one recursive crawl carries while running.
    Admission is the consumer that defines what a run offers -- dispatch reads the same object.
    """

    scheduler: DomainScheduler
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]]
    robots_allows: Callable[[NormalizedUrl], Awaitable[bool]]
    robots_crawl_delay: Callable[[NormalizedUrl], Awaitable[float | None]] | None
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult]
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]]
    spill: _SpillSink | None
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
    spill_write: Callable[[_SpillSink | None, int, EngineCrawlResult], Awaitable[None]]


class _BatchContext(NamedTuple):
    """Everything a batch-admission stage is allowed to see -- the ``(run, state,
    metrics)`` triple bundled so a stage can never be wired to one run's gate and
    another run's bookkeeping; built once by ``admit_recursive_batch``.
    """

    run: RecursiveRunState
    state: _BatchState
    metrics: MetricsStore


class _PinProvider(Protocol):
    """The one scheduler method admission reads: ``get_pinned_strategy``. Naming
    the surface, not the class, states the real requirement -- anything that can
    answer the pin lookup, the real scheduler and the test double alike.
    """

    def get_pinned_strategy(self, domain: str) -> str | None: ...


def _pinned_strategy(scheduler: _PinProvider, task: CrawlTask) -> str | None:
    """The strategy this task's domain is pinned to, or None when unpinned -- the
    one place per-domain pinning is read, previously a ``getattr`` guard spelled
    three ways under different local names.
    """
    return scheduler.get_pinned_strategy(task.domain)


def batch_is_http_heavy(
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


def _take_and_bucket(
    frontier: deque[tuple[int, CrawlTask]],
    scheduler: _PinProvider,
    limit: int,
) -> tuple[list[_Admitted], list[_Admitted]]:
    """Pop up to ``limit`` tasks, bucketing by tier in one pass.
    Returns ``(http_items, browser_items)``; the caller slices and returns excess.
    """
    http_items: list[_Admitted] = []
    browser_items: list[_Admitted] = []
    for _ in range(limit):
        if not frontier:
            break
        item = frontier.popleft()
        (browser_items if _pinned_strategy(scheduler, item[1]) in BROWSER_STRATEGIES else http_items).append(
            item
        )
    return http_items, browser_items


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
    """Resource-gate the frontier, then robots-filter whatever survived -- the
    only place the batch-admission context is assembled, so the stage protocol is
    stated once instead of re-spelled per stage.
    """
    ctx = _BatchContext(run, state, metrics)
    return await _robots_admitted_batch(ctx, await _resource_admitted_batch(ctx))


async def _resource_admitted_batch(ctx: _BatchContext) -> list[_Admitted]:
    run, state, metrics = ctx
    scale = run.resource_gate.batch_scale_factor()
    metrics.resource_memory_utilization = run.resource_gate.memory_ratio()
    metrics.resource_cpu_utilization = run.resource_gate.cpu_ratio()
    http_scale = getattr(run.resource_gate, "http_scale_factor", None)
    if http_scale is not None and batch_is_http_heavy(
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
    http_items, browser_items = _take_and_bucket(state.frontier, run.scheduler, batch_size)
    want_http, want_browser = len(http_items), len(browser_items)
    admit_http, admit_browser = await run.admission.admit(want_http, want_browser)
    if admit_http + admit_browser < batch_size:
        metrics.record_throttle()
        batch_size = admit_http + admit_browser
    selected = http_items[:admit_http] + browser_items[:admit_browser]
    excess = http_items[admit_http:] + browser_items[admit_browser:]
    for item in reversed(excess):
        state.frontier.appendleft(item)
    return selected


def _engine_envelope(job: CrawlTask, exc: BaseException) -> EngineCrawlResult:
    """Crawl-pipeline failure envelope for the domain scheduler, which is generic
    over the result shape: this pipeline supplies how a broken job is represented,
    reporting via ``CrawlTask.original_url``.
    """
    return failed_result_for(job, exc, url_of=lambda j: j.original_url)


def requeue_transient(
    task: CrawlTask,
    *,
    depth: int,
    frontier: deque[tuple[int, CrawlTask]],
    attempts: dict[NormalizedUrl, int],
) -> bool:
    """Re-queue a transiently-failed task to the frontier tail, up to the cap:
    returns True (the caller must not record a permanent failure) or False when
    the cap exhausts. Transience is the taxonomy's call; this owns only the bound.
    """
    count = attempts.get(task.normalized_url, 0)
    if count >= MAX_TRANSIENT_REQUEUES:
        return False
    attempts[task.normalized_url] = count + 1
    # Depth stays unchanged: a retry is not a deeper crawl, and the tail
    # re-queue preserves BFS order so one batch cannot starve the rest.
    frontier.append((depth, task))
    return True


# Self-managed concurrency ceiling (#self-tuning): per-batch concurrency ramps
# while the gate is open, which pulls it back under pressure.
RECURSIVE_RAMP_MAX: int = 128

# How far into the frontier we look to call the next batch cheap-HTTP or
# not; it matches the max achievable batch size we are about to size.
_HTTP_HEAVY_LOOKAHEAD: int = RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER


async def _spill_write(spill: _SpillSink | None, index: int, result: EngineCrawlResult) -> None:
    """Write a result to ``spill`` if present, awaiting the async seam (a sync
    ``write`` returning None is a test double and needs no await). ``None`` is a no-op.
    """
    if spill is None:
        return
    res = spill.write(index, result)
    if inspect.isawaitable(res):
        await res


@dataclass
class _RecursiveState:
    frontier: deque[tuple[int, CrawlTask]]
    visited: set[NormalizedUrl]
    next_index: int
    ramp: float
    page_count: int = 0
    transient_requeues: dict[NormalizedUrl, int] = field(default_factory=dict)

    def make_admitted_task(self, link: AdmittedLink) -> tuple[int, CrawlTask]:
        task = CrawlTask(
            index=self.next_index,
            original_url=link.original_url,
            normalized_url=link.normalized_url,
            domain=link.normalized_url.host,
        )
        self.next_index += 1
        return link.depth, task


async def _seed_recursive_frontier(
    run: RecursiveRunState,
    state: _RecursiveState,
    frontier: deque[tuple[int, CrawlTask]],
    seed_identities: set[NormalizedUrl],
) -> None:
    if not run.seed_sitemaps:
        return
    for seed in await run.sitemap_seeds():
        if seed in seed_identities:
            continue
        seed_identities.add(seed)
        task = CrawlTask(
            index=state.next_index,
            original_url=seed.raw,
            normalized_url=seed,
            domain=seed.site,
        )
        frontier.append((0, task))
        state.next_index += 1


async def _crawl_recursive_batch(
    run: RecursiveRunState,
    allowed_jobs: list[tuple[int, CrawlTask]],
) -> dict[int, EngineCrawlResult] | None:
    if not allowed_jobs:
        return {}
    return await run.scheduler.run(
        [job for _, job in allowed_jobs],
        run.worker,
        failed_result=_engine_envelope,
    )


async def _commit_recursive_result(
    run: RecursiveRunState,
    state: _RecursiveState,
    job: CrawlTask,
    depth: int,
    result: EngineCrawlResult,
) -> bool:
    if is_transient_classification(result.classification) and requeue_transient(
        job,
        depth=depth,
        frontier=state.frontier,
        attempts=state.transient_requeues,
    ):
        state.visited.discard(job.normalized_url)
        return False
    if not run.include_html:
        result = replace(result, html=None, text=None)
    await run.spill_write(run.spill, job.index, result)
    state.page_count += 1
    return True


async def _expand_recursive_result(
    run: RecursiveRunState,
    state: _RecursiveState,
    depth: int,
    result: EngineCrawlResult,
) -> None:
    await admit_discovered_links(
        result.discovered_links,
        source_depth=depth,
        max_depth=run.max_depth,
        max_pages=run.max_pages,
        known=state.visited,
        frontier=state.frontier,
        make_entry=state.make_admitted_task,
        scope_pattern=run.scope_pattern,
        want_document_bodies=run.want_document_bodies,
        respect_robots=run.respect_robots,
        robots_allows=run.robots_allows,
        robots_crawl_delay=run.robots_crawl_delay,
        commit_crawl_delay=lambda domain, delay: run.scheduler.set_domain_min_delay(domain, delay),
        host_policy=run.host_policy,
    )


async def _commit_recursive_results(
    run: RecursiveRunState,
    state: _RecursiveState,
    allowed_jobs: list[tuple[int, CrawlTask]],
    scheduled_results: dict[int, EngineCrawlResult] | None,
) -> None:
    scheduled_results = cast("dict[int, EngineCrawlResult]", scheduled_results)
    for depth, job in allowed_jobs:
        result = scheduled_results[job.index]
        if not await _commit_recursive_result(run, state, job, depth, result):
            continue
        if depth >= run.max_depth:
            continue
        await _expand_recursive_result(run, state, depth, result)


async def _start_recursive_state(
    run: RecursiveRunState,
    jobs: list[CrawlTask],
) -> _RecursiveState:
    state = _RecursiveState(
        frontier=deque((0, job) for job in jobs),
        visited=set(),
        next_index=len(jobs),
        ramp=float(run.global_concurrency),
    )
    seed_identities: set[NormalizedUrl] = {job.normalized_url for job in jobs}
    await _seed_recursive_frontier(run, state, state.frontier, seed_identities)
    return state


async def run_recursive_crawl(
    *,
    jobs: list[CrawlTask],
    scheduler: DomainScheduler,
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]],
    robots_allows: Callable[[NormalizedUrl], Awaitable[bool]],
    robots_crawl_delay: Callable[[NormalizedUrl], Awaitable[float | None]] | None = None,
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult],
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]],
    spill: _SpillSink | None,
    shutdown_event: asyncio.Event,
    max_depth: int,
    max_pages: int,
    respect_robots: bool,
    include_html: bool,
    want_document_bodies: bool,
    scope_pattern: BoundedPattern | None,
    global_concurrency: int,
    seed_sitemaps: bool,
    resource_gate: ResourceGate,
    admission: WeightedAdmission | None = None,
    host_policy: HostPolicy,
) -> None:
    """Bounded recursive BFS with self-tuning resource admission: writes results
    to ``spill`` and expands the frontier from each result's discovered links.
    The caller owns the closures, so this stays a pure dispatch function.
    """
    metrics = get_metrics()
    run = RecursiveRunState(
        scheduler=scheduler,
        worker=worker,
        robots_allows=robots_allows,
        robots_crawl_delay=robots_crawl_delay,
        robots_disallowed=robots_disallowed,
        sitemap_seeds=sitemap_seeds,
        spill=spill,
        max_depth=max_depth,
        max_pages=max_pages,
        respect_robots=respect_robots,
        include_html=include_html,
        want_document_bodies=want_document_bodies,
        scope_pattern=scope_pattern,
        global_concurrency=global_concurrency,
        seed_sitemaps=seed_sitemaps,
        resource_gate=resource_gate,
        admission=admission or default_admission_policy(resource_gate),
        host_policy=host_policy,
        ramp_max=RECURSIVE_RAMP_MAX,
        http_heavy_lookahead=_HTTP_HEAVY_LOOKAHEAD,
        spill_write=_spill_write,
    )
    state = await _start_recursive_state(run, jobs)
    while state.frontier and (max_pages <= 0 or state.page_count < max_pages):
        if shutdown_event.is_set():
            break
        allowed_jobs = await admit_recursive_batch(cast(Any, run), state, metrics)
        scheduled_results = await _crawl_recursive_batch(run, allowed_jobs)
        await _commit_recursive_results(run, state, allowed_jobs, scheduled_results)


__all__ = [
    "RECURSIVE_RAMP_MAX",
    "RecursiveRunState",
    "admit_recursive_batch",
    "batch_is_http_heavy",
    "requeue_transient",
    "run_recursive_crawl",
]
