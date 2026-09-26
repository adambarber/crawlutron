"""Resource and robots admission for one recursive crawl batch."""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from itertools import islice
from typing import Any, Protocol

from app.constants import (
    BROWSER_STRATEGIES,
    ENGINE_BATCH_CONCURRENCY_MULTIPLIER,
    HTTP_STRATEGIES,
)
from app.crawl.admission import gather_robots_decisions
from app.metrics import MetricsStore
from app.models import CrawlTask, EngineCrawlResult
from app.resource_pressure import ResourceGate, WeightedAdmission
from app.scheduler import DomainScheduler

logger = logging.getLogger(__name__)


class _BatchState(Protocol):
    frontier: deque[tuple[int, CrawlTask]]
    visited: set[str]
    ramp: float
    page_count: int


class _BatchRun(Protocol):
    scheduler: DomainScheduler
    robots_allows: Callable[[str], Awaitable[bool]]
    robots_crawl_delay: Callable[[str], Awaitable[float | None]] | None
    robots_disallowed: Callable[[CrawlTask], EngineCrawlResult]
    spill: Any
    max_pages: int
    respect_robots: bool
    global_concurrency: int
    resource_gate: ResourceGate
    admission: WeightedAdmission
    ramp_max: int
    http_heavy_lookahead: int
    spill_write: Callable[[Any, int, EngineCrawlResult], Awaitable[None]]


def _batch_is_http_heavy(
    scheduler: DomainScheduler,
    frontier: deque[tuple[int, CrawlTask]],
    sample: int,
) -> bool:
    if not frontier or sample <= 0:
        return False
    get_pin = getattr(scheduler, "get_pinned_strategy", None)
    counted = 0
    http_count = 0
    for _, task in islice(frontier, sample):
        if task.domain is None:
            continue
        counted += 1
        pinned = get_pin(task.domain) if get_pin is not None else None
        if pinned is not None and pinned in HTTP_STRATEGIES:
            http_count += 1
    return counted > 0 and http_count >= counted / 2


def _count_strategy_tiers(
    scheduler: DomainScheduler,
    frontier: deque[tuple[int, CrawlTask]],
    count: int,
) -> tuple[int, int]:
    get_pinned = getattr(scheduler, "get_pinned_strategy", None)
    http = 0
    browser = 0
    for _, task in islice(frontier, count):
        if get_pinned is None:
            http += 1
            continue
        pinned = get_pinned(task.domain)
        if pinned is not None and pinned in BROWSER_STRATEGIES:
            browser += 1
        else:
            http += 1
    return http, browser


def _pop_admitted(
    frontier: deque[tuple[int, CrawlTask]],
    admit_http: int,
    admit_browser: int,
    scheduler: DomainScheduler,
) -> list[tuple[int, CrawlTask]]:
    if admit_http + admit_browser == 0:
        return []
    get_pinned = getattr(scheduler, "get_pinned_strategy", None)
    taken: list[tuple[int, CrawlTask]] = []
    while frontier and len(taken) < admit_http + admit_browser:
        taken.append(frontier.popleft())
    http_tasks: list[tuple[int, CrawlTask]] = []
    browser_tasks: list[tuple[int, CrawlTask]] = []
    for item in taken:
        _depth, task = item
        is_browser = False
        if get_pinned is not None:
            pinned = get_pinned(task.domain)
            is_browser = pinned is not None and pinned in BROWSER_STRATEGIES
        (browser_tasks if is_browser else http_tasks).append(item)
    selected = http_tasks[:admit_http] + browser_tasks[:admit_browser]
    excess = http_tasks[admit_http:] + browser_tasks[admit_browser:]
    for item in reversed(excess):
        frontier.appendleft(item)
    return selected


async def _resource_admitted_batch(
    run: _BatchRun,
    state: _BatchState,
    metrics: MetricsStore,
) -> list[tuple[int, CrawlTask]]:
    scale = run.resource_gate.batch_scale_factor()
    metrics.resource_memory_utilization = run.resource_gate.memory_ratio()
    metrics.resource_cpu_utilization = run.resource_gate.cpu_ratio()
    http_scale = getattr(run.resource_gate, "http_scale_factor", None)
    if http_scale is not None and _batch_is_http_heavy(
        run.scheduler,
        state.frontier,
        run.http_heavy_lookahead,
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
    want_http, want_browser = _count_strategy_tiers(
        run.scheduler,
        state.frontier,
        batch_size,
    )
    admit_http, admit_browser = await run.admission.admit(want_http, want_browser)
    if admit_http + admit_browser < batch_size:
        metrics.record_throttle()
        batch_size = admit_http + admit_browser
    return _pop_admitted(state.frontier, admit_http, admit_browser, run.scheduler)


async def _robots_admitted_batch(
    run: _BatchRun,
    state: _BatchState,
    batch: list[tuple[int, CrawlTask]],
) -> list[tuple[int, CrawlTask]]:
    pending: list[tuple[int, CrawlTask]] = []
    seen_in_batch: set[str] = set()
    for depth, job in batch:
        if job.normalized_url in state.visited or job.normalized_url in seen_in_batch:
            continue
        seen_in_batch.add(job.normalized_url)
        pending.append((depth, job))
    decisions = (
        await gather_robots_decisions(
            [job.normalized_url for _, job in pending],
            run.robots_allows,
            run.robots_crawl_delay,
        )
        if run.respect_robots
        else [(True, None)] * len(pending)
    )
    allowed_jobs: list[tuple[int, CrawlTask]] = []
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
    run: _BatchRun,
    state: _BatchState,
    metrics: MetricsStore,
) -> list[tuple[int, CrawlTask]]:
    batch = await _resource_admitted_batch(run, state, metrics)
    return await _robots_admitted_batch(run, state, batch)


__all__ = ["admit_recursive_batch"]
