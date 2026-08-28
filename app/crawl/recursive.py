"""Recursive crawl dispatch (bounded BFS) with self-tuning admission.

Extracted from :mod:`app.crawl.engine.crawl_request` so the resource-gated
batch loop can be unit-tested in isolation, without standing up the whole
engine. The engine still owns policy parsing, the per-URL crawl chain, robots
handling, and result spill/reassembly; it hands this function the already-built
closures (worker, robots checks, sitemap seeding) and the dispatch knobs.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections import deque
from dataclasses import replace
from itertools import islice
from typing import Any, Awaitable, Callable

from app.constants import (
    ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
    HTTP_STRATEGIES,
    is_analytics_host,
    is_document_url,
)
from app.scheduler import DomainScheduler
from app.metrics import get_metrics
from app.models import CrawlTask, EngineCrawlResult
from app.resource_pressure import ResourceGate
from app.validation import registrable_domain

logger = logging.getLogger(__name__)

# Self-managed concurrency ceiling (#self-tuning): the recursive dispatcher
# ramps its per-batch concurrency up to this many base units while the resource
# gate is open, then the gate's scale factor pulls it back under pressure. This
# replaces a fixed operator-configured concurrency cap with automatic sizing to
# the cgroup budget. Kept modest so a single batch cannot overshoot the hard
# headroom before the next saturation check.
RECURSIVE_RAMP_MAX: int = 8

# How far into the frontier we look to decide whether the next batch is
# predominantly cheap HTTP work (item 7). Matches the max achievable batch size
# (ramp ceiling x concurrency multiplier) so we gauge the very batch we're about
# to size.
_HTTP_HEAVY_LOOKAHEAD: int = RECURSIVE_RAMP_MAX * ENGINE_BATCH_CONCURRENCY_MULTIPLIER


def _batch_is_http_heavy(
    scheduler: DomainScheduler,
    frontier: "deque[tuple[int, CrawlTask]]",
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


async def run_recursive_crawl(
    *,
    jobs: list[CrawlTask],
    scheduler: DomainScheduler,
    worker: Callable[[CrawlTask], Awaitable[EngineCrawlResult]],
    robots_allows: Callable[[str], Awaitable[bool]],
    robots_crawl_delay: "Callable[[str], Awaitable[float | None]] | None" = None,
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult],
    sitemap_seeds: Callable[[], Awaitable[list[str]]],
    spill: Any,
    shutdown_event: asyncio.Event,
    max_depth: int,
    max_pages: int,
    respect_robots: bool,
    include_html: bool,
    want_document_bodies: bool,
    scope_pattern: "re.Pattern[str] | None",
    global_concurrency: int,
    seed_sitemaps: bool,
    resource_gate: ResourceGate,
) -> None:
    """Bounded recursive BFS with self-tuning resource admission control.

    Writes page results (and robots-disallowed skips) to ``spill`` and expands
    the frontier from each result's discovered links. The caller owns the
    closures so this stays a pure dispatch function.
    """
    metrics = get_metrics()

    visited: set[str] = set()
    frontier: "deque[tuple[int, CrawlTask]]" = deque([(0, job) for job in jobs])
    next_index = len(jobs)
    page_count = 0
    # bounded recursive BFS (#11) seeded by sitemaps (#13)
    if seed_sitemaps:
        for seed in await sitemap_seeds():
            if seed in visited:
                continue
            frontier.append(
                (0, CrawlTask(index=next_index, original_url=seed, normalized_url=seed, domain=registrable_domain(seed)))
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
        _gate_wait_start = time.monotonic()
        await resource_gate.wait_while_saturated()
        _gate_waited = time.monotonic() - _gate_wait_start
        if _gate_waited > 1e-3:
            metrics.resource_pause_seconds_total += _gate_waited
        scale = resource_gate.batch_scale_factor()
        metrics.resource_memory_utilization = resource_gate.memory_ratio()
        metrics.resource_cpu_utilization = resource_gate.cpu_ratio()
        # #self-tuning HTTP-aware admission (item 7): browser crawls are the
        # memory hog (each ~300MB, only a few fit in the cgroup limit), which
        # throttles the whole batch even when the batch is mostly cheap HTTP
        # work and CPU has spare headroom. When the upcoming batch is
        # predominantly HTTP-pinned (domains proven to fetch without a browser),
        # consult the HTTP-aware scale so it rides spare CPU instead of being
        # dragged down by memory held by browser processes. Browser-bound and
        # cold (unpinned) batches keep the conservative combined gate.
        _http_scale = getattr(resource_gate, "http_scale_factor", None)
        if (
            _http_scale is not None
            and _batch_is_http_heavy(scheduler, frontier, _HTTP_HEAVY_LOOKAHEAD)
        ):
            scale = _http_scale()
        # #self-tuning first-batch warmup: the CPU gate has no real sample until
        # the first batch has actually run, so on a cold start it reports 0% and
        # would otherwise let the first batch launch global_concurrency *
        # multiplier parallel fetches -- which can briefly exceed a tight CPU
        # quota before admission engages. Until the gate is warm (or there is no
        # CPU ceiling to defend) cap the first batch at base concurrency; later
        # batches ramp to the multiplier under gate control.
        _cpu = getattr(resource_gate, "cpu", None)
        _concurrency_mult = ENGINE_BATCH_CONCURRENCY_MULTIPLIER
        if _cpu is not None and not _cpu.is_warm():
            _concurrency_mult = 1
        # Self-managing concurrency: the gate reports a single utilization
        # signal but cannot *create* work beyond the configured baseline, so we
        # ramp per-batch concurrency up while the gate is open (scale == 1.0,
        # i.e. under target) and let the gate's scale shrink it back under
        # pressure. The app thus sizes itself to the cgroup budget with no
        # operator-configured concurrency cap -- it rides up to the gate target
        # on its own. global_concurrency is the floor; RECURSIVE_RAMP_MAX the
        # ceiling (bounded so one batch cannot overshoot the hard headroom).
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
            _limited = (
                "cpu" if resource_gate.cpu_ratio() >= resource_gate.memory_ratio()
                else "memory"
            )
            logger.debug(
                "resource admission: throttling batch (scale=%.2f, limited_by=%s, "
                "mem=%.2f cpu=%.2f)",
                scale, _limited, metrics.resource_memory_utilization, metrics.resource_cpu_utilization,
            )
            batch_size = max(1, int(batch_size * scale))
            metrics.resource_throttle_batches_total += 1
        batch = [frontier.popleft() for _ in range(batch_size)]

        allowed_jobs: list[tuple[int, CrawlTask]] = []
        for depth, job in batch:
            if job.normalized_url in visited:
                continue
            if respect_robots and not await robots_allows(job.normalized_url):
                spill.write(job.index, robots_disallowed(job))
                visited.add(job.normalized_url)
                page_count += 1
                continue
            # Honor a host's robots Crawl-delay as a per-domain minimum delay
            # (adversarial perf #2.6). Applied on top of the scheduler's global/
            # adaptive pacing.
            if respect_robots and robots_crawl_delay is not None:
                delay = await robots_crawl_delay(job.normalized_url)
                if delay and delay > 0:
                    scheduler.set_domain_min_delay(job.domain, delay)
            allowed_jobs.append((depth, job))
            visited.add(job.normalized_url)

        scheduled_results = (
            await scheduler.run([job for _, job in allowed_jobs], worker)
            if allowed_jobs
            else []
        )

        for (depth, job), result in zip(allowed_jobs, scheduled_results):
            # Strip heavy payloads inside the loop so they are not retained
            # for the whole crawl when the caller did not ask for them (#1).
            if not include_html:
                result = replace(result, html=None, text=None)
            spill.write(job.index, result)
            page_count += 1
            if depth >= max_depth:
                continue
            for link in result.discovered_links:
                if link.normalized_url in visited:
                    continue
                if is_analytics_host(link.normalized_url):
                    continue
                if not want_document_bodies and is_document_url(link.normalized_url):
                    continue
                if scope_pattern is not None and not scope_pattern.search(link.normalized_url):
                    continue
                if respect_robots and not await robots_allows(link.normalized_url):
                    continue
                if respect_robots and robots_crawl_delay is not None:
                    delay = await robots_crawl_delay(link.normalized_url)
                    if delay and delay > 0:
                        scheduler.set_domain_min_delay(link.host, delay)
                if max_pages and page_count + len(frontier) >= max_pages:
                    break
                frontier.append(
                    (
                        depth + 1,
                        CrawlTask(index=next_index, original_url=link.url, normalized_url=link.normalized_url, domain=link.host),
                    )
                )
                next_index += 1
