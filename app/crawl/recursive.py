"""Recursive crawl dispatch (bounded BFS) with self-tuning admission.

Extracted from :mod:`app.crawl.engine.crawl_request` so the resource-gated
batch loop can be unit-tested in isolation, without standing up the whole
engine. The engine still owns policy parsing, the per-URL crawl chain, robots
handling, and result spill/reassembly; it hands this function the already-built
closures (worker, robots checks, sitemap seeding) and the dispatch knobs.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import replace
from itertools import islice
from typing import Any

from app.constants import (
    BROWSER_STRATEGIES,
    ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
    HTTP_STRATEGIES,
    is_analytics_host,
    is_document_url,
)
from app.crawl.result_helpers import _failed_result_for
from app.crawl.scope_match import scope_allows as _scope_allows
from app.metrics import get_metrics
from app.models import CrawlTask, EngineCrawlResult, NormalizedUrl
from app.resource_pressure import ResourceGate, WeightedAdmission, default_admission_policy
from app.scheduler import DomainScheduler
from app.validation import registrable_domain

# A pool-exhausted URL was never attempted: the page is fine, capacity was the
# problem. The recursive loop re-queues it onto the frontier tail so the
# at-least-once contract holds for capacity failures the same way it holds for
# crash recovery (memory #1947). The cap stops a permanently oversubscribed
# crawl from spinning one URL forever: after this many re-queues the URL fails
# for real and the crawl degrades instead of livelocking.
MAX_POOL_EXHAUSTION_REQUEUES = 3


def _engine_envelope(job: CrawlTask, exc: BaseException) -> EngineCrawlResult:
    """Crawl-pipeline failure envelope for the domain scheduler.

    ``DomainScheduler.run`` is generic over the result shape, so the pipeline
    that owns the ``EngineCrawlResult`` contract supplies how a broken job is
    represented. ``CrawlTask.original_url`` is the reporting URL for this
    pipeline; the render pipeline passes its own accessor for ``CrawlJob.url``.
    """
    return _failed_result_for(job, exc, url_of=lambda j: j.original_url)


def requeue_pool_exhausted(
    task: CrawlTask,
    *,
    depth: int,
    frontier: deque[tuple[int, CrawlTask]],
    attempts: dict[str, int],
) -> bool:
    """Re-queue a pool-exhausted task to the frontier tail, up to the cap.

    Returns True when the task was re-queued (the caller must NOT record it as
    a permanent failure) and False when the cap is exhausted (the caller lets
    the classified result stand). ``attempts`` maps the normalized URL to how
    many times it has already been re-queued and is mutated in place.
    """
    count = attempts.get(task.normalized_url, 0)
    if count >= MAX_POOL_EXHAUSTION_REQUEUES:
        return False
    attempts[task.normalized_url] = count + 1
    # Depth stays unchanged: a capacity retry is not a deeper crawl, and
    # re-queueing at the tail preserves BFS order so an oversubscribed crawl
    # does not starve the rest of the batch.
    frontier.append((depth, task))
    return True


logger = logging.getLogger(__name__)

# Self-managed concurrency ceiling (#self-tuning): the recursive dispatcher
# ramps its per-batch concurrency up to this many base units while the resource
# gate is open, then the gate's scale factor pulls it back under pressure. This
# replaces a fixed operator-configured concurrency cap with automatic sizing to
# the cgroup budget. Kept modest so a single batch cannot overshoot the hard
# headroom before the next saturation check.
RECURSIVE_RAMP_MAX: int = 128

# How far into the frontier we look to decide whether the next batch is
# predominantly cheap HTTP work (item 7). Matches the max achievable batch size
# (ramp ceiling x concurrency multiplier) so we gauge the very batch we're about
# to size.
_HTTP_HEAVY_LOOKAHEAD: int = RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER


def _batch_is_http_heavy(
    scheduler: DomainScheduler,
    frontier: deque[tuple[int, CrawlTask]],
    sample: int,
) -> bool:
    """True when the majority of the sampled frontier is HTTP-pinned work.

    A task counts as HTTP-heavy only when its domain was already pinned to an
    HTTP strategy (i.e. it has proven it can be fetched without a browser).
    Browser-pinned and cold (unpinned) domains keep the conservative combined
    memory+CPU gate: unpinned work may still escalate to a browser, and browser
    work is memory+CPU heavy. This is the safety that lets HTTP-heavy batches
    ride spare CPU headroom instead of being throttled by memory held by
    browser processes.
    """
    if not frontier or sample <= 0:
        return False
    get_pin = getattr(scheduler, "get_pinned_strategy", None)
    counted = 0
    http_count = 0
    # Slice the deque without materializing the whole frontier (islice avoids an
    # O(n) copy per batch on large crawls).
    for _, task in islice(frontier, sample):
        if task.domain is None:
            continue
        counted += 1
        pinned = get_pin(task.domain) if get_pin is not None else None
        if pinned is not None and pinned in HTTP_STRATEGIES:
            http_count += 1
    return counted > 0 and http_count >= counted / 2


async def _spill_write(spill: Any, index: int, result: Any) -> None:
    """Write a result to ``spill`` if present, tolerating sync or async ``write``.

    ``_ResultSpill.write`` is async (offloaded off the event loop, adversarial
    perf #1.3), but callers/tests may supply a sync spill object, so accept
    either. A ``None`` spill is a no-op (in-memory caller).
    """
    if spill is None:
        return
    res = spill.write(index, result)
    if inspect.isawaitable(res):
        await res


async def gather_robots_decisions(
    urls: list[str],
    robots_allows: Callable[[str], Awaitable[bool]],
    robots_crawl_delay: Callable[[str], Awaitable[float | None]] | None = None,
) -> list[tuple[bool, float | None]]:
    """Screen a batch of URLs against robots concurrently (perf throughput #1).

    Returns ``(allowed, crawl_delay)`` per URL in input order. The delay is
    only fetched for allowed URLs, mirroring the old sequential flow. Callers
    still route every check through the same ``robots_allows``/``robots_crawl_delay``
    closures, so the ``robots_cache`` chokepoint and SSRF semantics are unchanged;
    only the awaiting is concurrent. A same-origin concurrent miss may fetch
    robots.txt twice (idempotent ``put``); per-origin results stay identical.
    """

    async def _check(url: str) -> tuple[bool, float | None]:
        allowed = await robots_allows(url)
        delay: float | None = None
        if allowed and robots_crawl_delay is not None:
            delay = await robots_crawl_delay(url)
        return allowed, delay

    return list(await asyncio.gather(*(_check(url) for url in urls)))


def _count_strategy_tiers(scheduler, frontier, n):
    """Count HTTP vs browser tasks among the next ``n`` frontier entries.

    A task whose domain is pinned to a browser strategy is counted as browser;
    everything else (HTTP-pinned or cold/unpinned) is counted as HTTP -- we are
    optimistic that cold domains fetch without a browser and escalate later.
    """
    get_pinned = getattr(scheduler, "get_pinned_strategy", None)
    http = browser = 0
    for _, task in islice(frontier, n):
        if get_pinned is None:
            http += 1
            continue
        pinned = get_pinned(task.domain)
        if pinned is not None and pinned in BROWSER_STRATEGIES:
            browser += 1
        else:
            http += 1
    return http, browser


def _pop_admitted(frontier, admit_h, admit_b, scheduler):
    """Pop up to ``admit_h + admit_b`` tasks, HTTP-first, capping browser tasks.

    Excess (not admitted this batch) is returned to the front of the frontier so
    it is retried on the next batch rather than dropped.
    """
    if admit_h + admit_b == 0:
        return []
    get_pinned = getattr(scheduler, "get_pinned_strategy", None)
    taken: list[tuple[int, CrawlTask]] = []
    while frontier and len(taken) < admit_h + admit_b:
        taken.append(frontier.popleft())
    http_tasks: list = []
    browser_tasks: list = []
    for item in taken:
        _, task = item
        is_browser = False
        if get_pinned is not None:
            pinned = get_pinned(task.domain)
            is_browser = pinned is not None and pinned in BROWSER_STRATEGIES
        (browser_tasks if is_browser else http_tasks).append(item)
    selected = http_tasks[:admit_h] + browser_tasks[:admit_b]
    excess = http_tasks[admit_h:] + browser_tasks[admit_b:]
    for item in reversed(excess):
        frontier.appendleft(item)
    return selected


async def run_recursive_crawl(
    *,
    jobs: list[CrawlTask],
    scheduler: DomainScheduler,
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]],
    robots_allows: Callable[[str], Awaitable[bool]],
    robots_crawl_delay: Callable[[str], Awaitable[float | None]] | None = None,
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult],
    sitemap_seeds: Callable[[], Awaitable[list[NormalizedUrl]]],
    spill: Any,
    shutdown_event: asyncio.Event,
    max_depth: int,
    max_pages: int,
    respect_robots: bool,
    include_html: bool,
    want_document_bodies: bool,
    scope_pattern: re.Pattern[str] | None,
    global_concurrency: int,
    seed_sitemaps: bool,
    resource_gate: ResourceGate,
    admission: WeightedAdmission | None = None,
) -> None:
    """Bounded recursive BFS with self-tuning resource admission control.

    Writes page results (and robots-disallowed skips) to ``spill`` and expands
    the frontier from each result's discovered links. The caller owns the
    closures so this stays a pure dispatch function.
    """
    metrics = get_metrics()

    admission = admission or default_admission_policy(resource_gate)

    visited: set[str] = set()
    # How many times each URL has been re-queued for pool exhaustion, so the
    # cap in requeue_pool_exhausted can bound a permanently oversubscribed crawl.
    pool_exhaustion_attempts: dict[str, int] = {}
    frontier: deque[tuple[int, CrawlTask]] = deque([(0, job) for job in jobs])
    next_index = len(jobs)
    page_count = 0
    # bounded recursive BFS (#11) seeded by sitemaps (#13)
    if seed_sitemaps:
        for seed in await sitemap_seeds():
            if seed in visited:
                continue
            frontier.append(
                (
                    0,
                    CrawlTask(
                        index=next_index,
                        original_url=seed,
                        normalized_url=seed,
                        domain=registrable_domain(seed),
                    ),
                )
            )
            next_index += 1
    _ramp = float(global_concurrency)
    while frontier and page_count < max_pages:
        if shutdown_event.is_set():
            break
        # Pull a batch sized to the configured concurrency (and the remaining
        # page budget) so discovered links are fetched *through the scheduler*
        # concurrently instead of strictly one-by-one. Without this the
        # recursive crawl ignores global_concurrency entirely.
        # #self-tuning: ride at the resource targets instead of OOMing (memory)
        # or tripping the kernel CPU throttle (CPU). Above either hard ceiling
        # dispatch pauses until usage drains; in the soft zone batches shrink
        # proportionally to the most-contended resource.
        # Resource signals (kept for metrics + the HTTP-aware two-tier scale).
        scale = resource_gate.batch_scale_factor()
        metrics.resource_memory_utilization = resource_gate.memory_ratio()
        metrics.resource_cpu_utilization = resource_gate.cpu_ratio()
        # #self-tuning HTTP-aware admission (item 7): browser crawls are the
        # memory hog, which would otherwise throttle the whole batch. When the
        # upcoming batch is predominantly HTTP-pinned, ride the CPU-aware HTTP
        # scale instead of the memory-held-by-browsers combined gate.
        _http_scale = getattr(resource_gate, "http_scale_factor", None)
        if _http_scale is not None and _batch_is_http_heavy(scheduler, frontier, _HTTP_HEAVY_LOOKAHEAD):
            scale = _http_scale()
        # #self-tuning first-batch warmup: until the CPU gate has a sample, cap
        # the first batch at base concurrency.
        _cpu = getattr(resource_gate, "cpu", None)
        _concurrency_mult = ENGINE_BATCH_CONCURRENCY_MULTIPLIER
        if _cpu is not None and not _cpu.is_warm():
            _concurrency_mult = 1
        # Self-managing concurrency: ramp per-batch concurrency up while the gate
        # is open, let the gate's scale shrink it under pressure. global_concurrency
        # is the floor; RECURSIVE_RAMP_MAX the ceiling (bounded so one batch
        # cannot overshoot the hard headroom). The weighted admission below is
        # the real RAM/CPU backstop.
        if scale >= 1.0:
            _ramp = min(float(RECURSIVE_RAMP_MAX), _ramp * 1.10)
        else:
            _ramp = max(float(global_concurrency), _ramp * scale)
        _base = max(1, int(_ramp))
        batch_size = min(
            len(frontier),
            max(1, _base * _concurrency_mult),
            max(1, max_pages - page_count),
        )
        if scale < 1.0:
            _limited = "cpu" if resource_gate.cpu_ratio() >= resource_gate.memory_ratio() else "memory"
            logger.debug(
                "resource admission: throttling batch (scale=%.2f, limited_by=%s, mem=%.2f cpu=%.2f)",
                scale,
                _limited,
                metrics.resource_memory_utilization,
                metrics.resource_cpu_utilization,
            )
            batch_size = max(1, int(batch_size * scale))
            metrics.resource_throttle_batches_total += 1
        # Two-tier weighted admission (replaces the stop-the-world pause): grant
        # as many HTTP tasks as fit the live RAM/CPU headroom, then up to the
        # browser-tier cap. If nothing fits we yield briefly (backpressure)
        # rather than parking the whole crawl, so the cheap HTTP path scales to
        # hundreds of concurrent pages while the browser tier stays RAM-budgeted.
        _want_http, _want_browser = _count_strategy_tiers(scheduler, frontier, batch_size)
        _admit_h, _admit_b = await admission.admit(_want_http, _want_browser)
        if _admit_h + _admit_b < batch_size:
            metrics.resource_throttle_batches_total += 1
            batch_size = _admit_h + _admit_b
        batch = _pop_admitted(frontier, _admit_h, _admit_b, scheduler)

        # Dedupe the batch against visited AND itself (in input order) before
        # screening: the old sequential loop marked visited as it went, so a
        # repeated URL in one batch was crawled once. The concurrent screen
        # below must see the same de-duplicated set.
        pending: list[tuple[int, CrawlTask]] = []
        seen_in_batch: set[str] = set()
        for depth, job in batch:
            if job.normalized_url in visited or job.normalized_url in seen_in_batch:
                continue
            seen_in_batch.add(job.normalized_url)
            pending.append((depth, job))
        if respect_robots:
            decisions = await gather_robots_decisions(
                [job.normalized_url for _, job in pending], robots_allows, robots_crawl_delay
            )
        else:
            decisions = [(True, None)] * len(pending)
        allowed_jobs: list[tuple[int, CrawlTask]] = []
        for (depth, job), (allowed, delay) in zip(pending, decisions, strict=True):
            if respect_robots and not allowed:
                await _spill_write(spill, job.index, robots_disallowed(job))
                visited.add(job.normalized_url)
                page_count += 1
                continue
            # Honor a host's robots Crawl-delay as a per-domain minimum delay
            # (adversarial perf #2.6). Applied on top of the scheduler's global/
            # adaptive pacing. Applied in batch order so same-domain races
            # resolve deterministically (last write wins, as before).
            if respect_robots and delay and delay > 0:
                scheduler.set_domain_min_delay(job.domain, delay)
            allowed_jobs.append((depth, job))
            visited.add(job.normalized_url)

        scheduled_results = (
            await scheduler.run(
                [job for _, job in allowed_jobs],
                worker,
                failed_result=_engine_envelope,
            )
            if allowed_jobs
            else {}
        )

        for depth, job in allowed_jobs:
            # Align each result with its own job by index. ``scheduler.run``
            # reports a ``job.index -> result`` mapping (completion order is
            # arbitrary), so a positional zip against ``allowed_jobs`` -- which
            # ``_pop_admitted`` has already tier-reordered (HTTP first) -- would
            # write each result to the wrong spill index whenever HTTP and
            # browser tasks interleave, and shift every subsequent pairing after
            # a failed job (B1).
            #
            # A job can never be absent from the mapping: the scheduler's
            # worker-exception chokepoint builds a classified terminal result
            # for any worker that raised.
            # ``scheduled_results`` is None only when the scheduler was asked to
            # deliver nothing; this branch runs inside the loop that consumed the
            # same jobs list, so a mapping is present by construction. Stating it
            # locally keeps the lookup honest without a broad ``assert``.
            assert scheduled_results is not None
            result = scheduled_results[job.index]
            # A pool-exhausted URL was never attempted: re-queue it to the
            # frontier tail under the per-job cap (at-least-once for capacity
            # failures) instead of recording a permanent page failure.
            if result.classification == "browser_pool_exhausted" and requeue_pool_exhausted(
                job, depth=depth, frontier=frontier, attempts=pool_exhaustion_attempts
            ):
                visited.discard(job.normalized_url)
                # No page was crawled: the URL goes back on the frontier for a
                # retry, so this must NOT consume a max_pages slot. Counting it
                # here let a burst of pool-exhaustion events terminate the crawl
                # before its real page budget was spent.
                continue
            # Strip heavy payloads inside the loop so they are not retained
            # for the whole crawl when the caller did not ask for them (#1).
            if not include_html:
                result = replace(result, html=None, text=None)
            await _spill_write(spill, job.index, result)
            page_count += 1
            if depth >= max_depth:
                continue
            # Sync pre-filter first (visited/analytics/document/scope need no
            # I/O), then screen the survivors' robots checks concurrently.
            # Survivors past the max_pages break still get screened: the old
            # sequential loop set crawl-delays before the break check too, and
            # the extra checks are robots-cache hits in the common case.
            candidates = []
            for link in result.discovered_links:
                if link.normalized_url in visited:
                    continue
                if is_analytics_host(link.normalized_url):
                    continue
                if not want_document_bodies and is_document_url(link.normalized_url):
                    continue
                if scope_pattern is not None and not _scope_allows(scope_pattern, link.normalized_url):
                    continue
                candidates.append(link)
            if respect_robots:
                link_decisions = await gather_robots_decisions(
                    [link.normalized_url for link in candidates], robots_allows, robots_crawl_delay
                )
            else:
                link_decisions = [(True, None)] * len(candidates)
            for link, (link_allowed, link_delay) in zip(candidates, link_decisions, strict=True):
                if respect_robots and not link_allowed:
                    continue
                if respect_robots and link_delay and link_delay > 0:
                    scheduler.set_domain_min_delay(link.host, link_delay)
                if max_pages and page_count + len(frontier) >= max_pages:
                    break
                frontier.append(
                    (
                        depth + 1,
                        CrawlTask(
                            index=next_index,
                            original_url=link.url,
                            normalized_url=link.normalized_url,
                            domain=link.host,
                        ),
                    )
                )
                next_index += 1
