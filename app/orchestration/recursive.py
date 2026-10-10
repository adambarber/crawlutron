"""Recursive crawl dispatch (bounded BFS) with self-tuning admission.

Extracted from :mod:`app.orchestration.engine.crawl_request` so the resource-gated
batch loop can be unit-tested in isolation, without standing up the whole
engine. The engine still owns policy parsing, the per-URL crawl chain, robots
handling, and result spill/reassembly; it hands this function the already-built
closures (worker, robots checks, sitemap seeding) and the dispatch knobs.
"""

from __future__ import annotations

import asyncio
import inspect
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from typing import Any, cast

from app.admission.admission import (
    AdmittedLink,
    admit_discovered_links,
)
from app.admission.recursive_admission import RecursiveRunState, admit_recursive_batch
from app.models.crawl_task import CrawlTask
from app.models.engine_crawl_result import EngineCrawlResult
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.orchestration.result_helpers import failed_result_for
from app.orchestration.scheduler import DomainScheduler
from app.runtime.metrics import get_metrics
from app.runtime.resource_pressure import ResourceGate, WeightedAdmission, default_admission_policy
from app.safety.safe_regex import BoundedPattern
from app.vocabulary.limits import ENGINE_BATCH_CONCURRENCY_MULTIPLIER
from app.vocabulary.taxonomy import is_transient_classification

# A transiently-failed URL is not a dead page: our fetch died, or capacity was
# the problem, so the page itself is still worth having. The recursive loop
# re-queues such a task onto the frontier tail so the at-least-once contract
# holds for a DNS blip and an exhausted renderer pool alike. The
# cap stops a permanently unreachable host from spinning one URL forever: after
# this many re-queues the URL fails for real and the crawl degrades instead of
# livelocking. Which failures qualify is the taxonomy's call, not a local list
# -- see ``app.vocabulary.taxonomy.is_transient_classification``.
MAX_TRANSIENT_REQUEUES = 3


def _engine_envelope(job: CrawlTask, exc: BaseException) -> EngineCrawlResult:
    """Crawl-pipeline failure envelope for the domain scheduler.

    ``DomainScheduler.run`` is generic over the result shape, so the pipeline
    that owns the ``EngineCrawlResult`` contract supplies how a broken job is
    represented. ``CrawlTask.original_url`` is the reporting URL for this
    pipeline; the render pipeline passes its own accessor for ``CrawlJob.url``.
    """
    return failed_result_for(job, exc, url_of=lambda j: j.original_url)


def requeue_transient(
    task: CrawlTask,
    *,
    depth: int,
    frontier: deque[tuple[int, CrawlTask]],
    attempts: dict[NormalizedUrl, int],
) -> bool:
    """Re-queue a transiently-failed task to the frontier tail, up to the cap.

    Returns True when the task was re-queued (the caller must NOT record it as
    a permanent failure) and False when the cap is exhausted (the caller lets
    the classified result stand). ``attempts`` maps the normalized URL to how
    many times it has already been re-queued and is mutated in place.

    Whether a classification is transient at all is the taxonomy's call (see
    ``app.vocabulary.taxonomy.is_transient_classification``); this function owns only the
    bound, so the cap cannot be sidestepped by a caller re-queueing for its own
    reason.
    """
    count = attempts.get(task.normalized_url, 0)
    if count >= MAX_TRANSIENT_REQUEUES:
        return False
    attempts[task.normalized_url] = count + 1
    # Depth stays unchanged: a retry is not a deeper crawl, and re-queueing at
    # the tail preserves BFS order so an oversubscribed crawl does not starve
    # the rest of the batch.
    frontier.append((depth, task))
    return True


# Self-managed concurrency ceiling (#self-tuning): the recursive dispatcher
# ramps its per-batch concurrency up to this many base units while the resource
# gate is open, then the gate's scale factor pulls it back under pressure. This
# replaces a fixed operator-configured concurrency cap with automatic sizing to
# the cgroup budget. Kept modest so a single batch cannot overshoot the hard
# headroom before the next saturation check.
RECURSIVE_RAMP_MAX: int = 128

# How far into the frontier we look to decide whether the next batch is
# predominantly cheap HTTP work. Matches the max achievable batch size
# (ramp ceiling x concurrency multiplier) so we gauge the very batch we're about
# to size.
_HTTP_HEAVY_LOOKAHEAD: int = RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER


async def _spill_write(spill: Any, index: int, result: Any) -> None:
    """Write a result to ``spill`` if present, tolerating sync or async ``write``.

    ``_ResultSpill.write`` is async (offloaded off the event loop), but
    callers/tests may supply a sync spill object, so accept
    either. A ``None`` spill is a no-op (in-memory caller).
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
    await _spill_write(run.spill, job.index, result)
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
    spill: Any,
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
    """Bounded recursive BFS with self-tuning resource admission control.

    Writes page results (and robots-disallowed skips) to ``spill`` and expands
    the frontier from each result's discovered links. The caller owns the
    closures so this stays a pure dispatch function.
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
