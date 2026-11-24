"""Background whole-site job runner for ``/v1/jobs``."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Final, cast

from crawlutron.admission.admission import (
    JobAdmissionContext,
    admit_job_links,
    admitted_page_identities,
    fetch_job_sitemap_candidates,
    record_cap_overcommitted,
    record_cap_truncated,
)
from crawlutron.admission.admission_links import remaining_page_capacity
from crawlutron.admission.policy import read_policy
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.crawl_result import EngineCrawlResult
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.runtime import process_state
from crawlutron.service.crawl_executor import execute_crawl as _real_execute_crawl
from crawlutron.service.effects import EffectContainer, EffectOutcome, run_crawl_with_claim
from crawlutron.service.job_batch import absorb_batch
from crawlutron.service.job_lifecycle import (
    cancel_all_tasks,
    cancel_job_task,
    is_running,
    shutdown_job_runner,
    start_tracked_task,
    tracked_task,
)
from crawlutron.service.job_recovery import (
    pause_for_recovery,
    recover_and_start_pending_jobs,
    recover_orphaned_running_jobs,
)
from crawlutron.service.job_runner_lease import new_claim_token
from crawlutron.service.job_runner_state import (
    load_and_claim,
    mark_complete_if_empty,
    should_stop,
)
from crawlutron.storage.job_store import (
    DEFAULT_JOB_CLAIM_SECONDS,
    CrawlJobState,
    FrontierEntry,
    JobStatus,
    JobStore,
    job_max_pages,
    normalize_job_state,
)
from crawlutron.storage.safe_db import StorageError
from crawlutron.transport.robots import FetchAndDecode
from crawlutron_admit import Count, Duration
from crawlutron_admit.task_scope import Grace, NamedRegistry, ScopeRefused, TaskScope
from safe_url.normalized_url import NormalizedUrl, admit_or_none
from safe_url.url_redaction import redact_url

logger = logging.getLogger(__name__)

BATCH_SIZE = 8

_STORAGE_RETRY_ATTEMPTS: Final[int] = 3  # then PAUSED for recovery, never terminal FAILED
_STORAGE_RETRY_BACKOFF_SECONDS: Final[float] = 0.01


ExecuteCrawl = Callable[..., Awaitable[ProducerEnvelope]]


_URL_IN_TEXT = re.compile(r"https?://[^\s'\"<>]+")


def _scrubbed(error: str) -> str:
    return _URL_IN_TEXT.sub(lambda m: redact_url(m.group(0)), error)


_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(
    job_store: JobStore, job_id: str, error: str, *, claimed_by: str | None = None
) -> bool:
    scrubbed = _scrubbed(error)
    if claimed_by is not None:
        return await job_store.aset_status(
            job_id,
            JobStatus.FAILED,
            expected=JobStatus.RUNNING,
            error=scrubbed,
            claimed_by=claimed_by,
            require_owner=True,
            clear_claim=True,
        )
    return await job_store.aset_status(job_id, JobStatus.FAILED, expected=JobStatus.RUNNING, error=scrubbed)


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


def _has() -> bool:
    """Whether a non-default execute_crawl is installed (leaked state)."""
    return _execute_crawl is not _real_execute_crawl


process_state.register(__name__, reset_execute_crawl, _has)


def pop_frontier(job: CrawlJobState) -> list[FrontierEntry]:
    normalize_job_state(job)
    max_pages = job_max_pages(job.policy, job.tier)
    if job.in_flight:
        if max_pages > 0:
            fixed_count = len(job.visited) + sum(1 for entry in job.in_flight if entry.url not in job.visited)
            if fixed_count > max_pages:
                record_cap_overcommitted(job)
        return list(job.in_flight[:BATCH_SIZE])
    capacity = remaining_page_capacity(len(job.visited), max_pages)
    if capacity is not None and capacity <= 0:
        if job.frontier:
            record_cap_truncated(job)
        job.frontier_removed.extend(entry.url for entry in job.frontier)
        del job.frontier[:]
        return []
    count = min(BATCH_SIZE, len(job.frontier))
    if capacity is not None:
        count = min(count, capacity)
    if capacity is not None and count == capacity and count < len(job.frontier):
        record_cap_truncated(job)
        job.frontier_removed.extend(entry.url for entry in job.frontier[count:])
        del job.frontier[count:]
    batch = list(job.frontier[:count])
    job.frontier_removed.extend(entry.url for entry in batch)
    job.frontier_mark = max(0, job.frontier_mark - count)
    del job.frontier[:count]
    return batch


def job_engine_payload(job: CrawlJobState, batch: Sequence[FrontierEntry]) -> CrawlRequest:
    urls = [entry.url.raw for entry in batch]
    policy = read_policy(job.policy, job.tier)
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return CrawlRequest(urls=urls, policy=policy)


def job_batch_timeout_seconds(payload: CrawlRequest) -> float:
    """Per-URL policy budget across the batch's URLs."""
    url_count = len(payload.urls) if payload.urls is not None else 1
    return payload.policy.limits.total_url_budget_seconds * url_count


async def _seed_job_sitemaps(
    job: CrawlJobState, context: JobAdmissionContext, container: EffectContainer
) -> None:
    candidates = await fetch_job_sitemap_candidates(job.root_url, context, container)
    outcome = await admit_job_links(job, candidates, source_depth=-1, context=context)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + outcome.accepted


def _resolve_redirects(
    items: Sequence[EngineCrawlResult], resolved: dict[NormalizedUrl, EngineCrawlResult]
) -> None:
    for produced in items:
        for key in (produced.final_url, produced.normalized_url):
            if key is not None:
                resolved.setdefault(key, produced)


def _resolved_items(
    items: Sequence[EngineCrawlResult], host_policy
) -> dict[NormalizedUrl, EngineCrawlResult]:
    resolved: dict[NormalizedUrl, EngineCrawlResult] = {}
    for produced in items:
        admitted = admit_or_none(produced.original_url, policy=host_policy)
        if admitted is not None:
            resolved.setdefault(admitted, produced)
    _resolve_redirects(items, resolved)
    return resolved


async def _renew_claim_and_save(job_store: JobStore, job_id: str, token: str, job: CrawlJobState) -> bool:
    if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
        return False
    await job_store.asave(job)
    return True


async def _run_batch(
    job: CrawlJobState,
    batch: Sequence[FrontierEntry],
    context: JobAdmissionContext,
    known: set[NormalizedUrl],
    *,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    container: EffectContainer,
    job_store: JobStore,
    job_id: str,
    token: str,
) -> CrawlJobState | None:
    """Run one batch under its policy-derived bound and absorb what it resolved.

    None means the job stops here: the claim was lost, the job was paused or
    cancelled, or the state could not be reloaded.
    """
    payload = job_engine_payload(job, batch)
    timeout = job_batch_timeout_seconds(payload)

    # The default binds this batch's payload: the closure must not read the
    # loop variable, and the payload chokepoint checks the builder's name.
    async def _crawl_op(payload: CrawlRequest = payload) -> object:
        return await _execute_crawl(payload, settings=settings, shutdown_event=shutdown_event)

    claim = await run_crawl_with_claim(
        container, _crawl_op, batch[0].url.raw, timeout, job_store, job_id, token
    )
    reloaded = await job_store.areload_state(job_id)
    if reloaded is None or reloaded.claimed_by != token:
        return None
    job = replace(reloaded, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
    resolved = _resolved_items(cast("Sequence[EngineCrawlResult]", claim.items), job.host_policy)
    timed_out = claim.outcome is EffectOutcome.TIMED_OUT
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        if job.in_flight:
            await absorb_batch(job, batch, resolved, context, known, timed_out=timed_out)
            await job_store.asave(job)
        return None
    if not claim.still_owned or job.status is not JobStatus.RUNNING:
        return None
    await absorb_batch(job, batch, resolved, context, known, timed_out=timed_out)
    if not await _renew_claim_and_save(job_store, job_id, token, job):
        return None
    return job


async def _run_claimed_loop(
    job,
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    context,
    token: str,
    container: EffectContainer,
) -> None:
    known = admitted_page_identities(job, host_policy=context.host_policy)
    while True:
        if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
            return
        state = await job_store.areload_state(job_id)
        if state is None:
            return
        job = replace(state, results=job.results, visited=job.visited, visited_mark=job.visited_mark)
        if await should_stop(job, job_id, job_store, shutdown_event, claim_token=token):
            return
        batch = pop_frontier(job)
        if await mark_complete_if_empty(job, job_id, job_store, batch, claim_token=token):
            return
        if not job.in_flight:
            job.in_flight = list(batch)
        if not await _renew_claim_and_save(job_store, job_id, token, job):
            return
        updated = await _run_batch(
            job,
            batch,
            context,
            known,
            settings=settings,
            shutdown_event=shutdown_event,
            container=container,
            job_store=job_store,
            job_id=job_id,
            token=token,
        )
        if updated is None:
            return
        job = updated


async def _run_job_body(
    job: CrawlJobState,
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    fetch_and_decode: FetchAndDecode | None,
    token: str,
) -> None:
    """One job attempt: context, optional sitemap seed, claim loop.
    Retried on StorageError, resuming each pass from the persisted row."""
    context = JobAdmissionContext.create(job.policy, job.tier, settings, fetch_and_decode)
    container = EffectContainer()
    if context.max_depth > 0 and context.seed_sitemaps:
        await _seed_job_sitemaps(job, context, container)
        await job_store.asave(job)
    await _run_claimed_loop(job, job_id, job_store, settings, shutdown_event, context, token, container)


async def run_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    fetch_and_decode: FetchAndDecode | None = None,
    *,
    preclaimed: bool = False,
    claim_token: str | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> None:
    token = claim_token or new_claim_token(job_id)
    job = await load_and_claim(
        job_id, job_store, preclaimed=preclaimed, claim_token=token, claim_seconds=claim_seconds
    )
    if job is None:
        return
    try:
        last_error: StorageError | TimeoutError | None = None
        for _attempt in range(_STORAGE_RETRY_ATTEMPTS):
            try:
                await _run_job_body(job, job_id, job_store, settings, shutdown_event, fetch_and_decode, token)
                return
            except (StorageError, TimeoutError) as exc:
                last_error = exc
                if isinstance(exc, TimeoutError) or _attempt + 1 == _STORAGE_RETRY_ATTEMPTS:
                    break
                await asyncio.sleep(_STORAGE_RETRY_BACKOFF_SECONDS)
        await pause_for_recovery(
            job_store, job_id, token, _scrubbed(str(last_error)) if last_error is not None else None
        )
        return
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        await arecord_terminal_failure(job_store, job_id, str(exc), claimed_by=token)


async def _transition(
    job_store: JobStore, job_id: str, to: JobStatus, *, expected: JobStatus | frozenset[JobStatus]
) -> str:
    if await job_store.aset_status(job_id, to, expected=expected):
        return "ok"
    return "missing" if await job_store.aload(job_id) is None else "conflict"


def reset_job_runner() -> None:
    cancel_all_tasks()
    reset_execute_crawl()


def start_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    *,
    preclaimed: bool = False,
    claim_token: str | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> asyncio.Task:
    token = claim_token or new_claim_token(job_id)
    return start_tracked_task(
        job_id,
        lambda: run_job(
            job_id,
            job_store,
            settings,
            shutdown_event,
            preclaimed=preclaimed,
            claim_token=token,
            claim_seconds=claim_seconds,
        ),
    )


async def pause_job(job_id: str, job_store: JobStore) -> str:
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(
    job_id: str,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    wedge_grace_seconds: float = 5.0,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> str:
    token = new_claim_token(job_id)
    outcome = "conflict"
    if await job_store.aset_status(
        job_id,
        JobStatus.RUNNING,
        expected=JobStatus.PAUSED,
        claimed_by=token,
        claim_now=time.time(),
        claim_seconds=claim_seconds,
    ):
        outcome = "ok"
    elif await job_store.aload(job_id) is None:
        outcome = "missing"
    if outcome == "ok":
        old = tracked_task(job_id)
        if old is not None:
            try:
                await asyncio.wait_for(asyncio.shield(old), timeout=wedge_grace_seconds)
            except TimeoutError:
                scope = TaskScope.process(
                    registry=NamedRegistry("process:job-retire", Count(1), Duration(0.25)),
                    grace=Grace(wedge_grace_seconds),
                )
                handle = scope.adopt(old)
                with contextlib.suppress(ScopeRefused):
                    await scope.stop(handle, Grace(wedge_grace_seconds))
        start_job(
            job_id,
            job_store,
            settings,
            shutdown_event,
            preclaimed=True,
            claim_token=token,
            claim_seconds=claim_seconds,
        )
    return outcome


async def cancel_job(job_id: str, job_store: JobStore) -> str:
    return await _transition(
        job_store,
        job_id,
        JobStatus.CANCELLED,
        expected=frozenset({JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PENDING}),
    )


__all__ = [
    "BATCH_SIZE",
    "cancel_job",
    "cancel_job_task",
    "install_execute_crawl",
    "is_running",
    "pause_job",
    "recover_orphaned_running_jobs",
    "run_job",
    "recover_and_start_pending_jobs",
    "reset_job_runner",
    "resume_job",
    "shutdown_job_runner",
    "start_job",
    "start_tracked_task",
]
