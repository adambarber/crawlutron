"""Background whole-site job runner for ``/v1/jobs``.

Consumes the persisted frontier in persistent batches through the shared
engine; every transition leaves the store resumable."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import Any

from app.admission.admission import (
    JobAdmissionContext,
    admit_job_links,
    fetch_job_sitemap_candidates,
    job_max_pages,
    record_cap_overcommitted,
    record_cap_truncated,
    requeue_admitted_pages,
)
from app.admission.admission_links import remaining_page_capacity
from app.admission.policy import read_policy
from app.api.crawl_executor import execute_crawl as _real_execute_crawl
from app.api.job_lifecycle import (
    cancel_job_task,
    is_running,
    reset_job_runner,
    shutdown_job_runner,
    start_job,
    start_tracked_task,
)
from app.api.job_recovery import (
    recover_and_start_pending_jobs,
    recover_orphaned_running_jobs,
)
from app.api.response_shaping import failed_unified_result
from app.api.unified_shaping import abuild_unified_result
from app.models.crawl_result import CrawlResult, EngineCrawlResult
from app.models.crawl_task import CrawlTask
from app.models.normalized_url import NormalizedUrl, admit_or_none
from app.models.result_record import ProducerEnvelope
from app.models.taxonomy import Classification
from app.orchestration.render_result import terminal_attempt
from app.orchestration.robots import FetchAndDecode
from app.storage.job_store import (
    FrontierEntry,
    JobStatus,
    JobStore,
    normalize_job_state,
)
from app.vocabulary.limits import MAX_URL_TRANSIENT_ATTEMPTS
from app.vocabulary.taxonomy import is_transient_classification, status_for_classification

logger = logging.getLogger(__name__)


BATCH_SIZE = 8


ExecuteCrawl = Callable[..., Awaitable[ProducerEnvelope]]


_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


async def arecord_terminal_failure(job_store: JobStore, job_id: str, error: str) -> None:
    """Stamp ``error`` on a RUNNING job and flip it to FAILED.

    A job past RUNNING is left as-is, so a late batch failure never
    overwrites an operator's cancel/pause."""
    job = await job_store.aload(job_id)
    if job is None or job.status is not JobStatus.RUNNING:
        return
    job.error = error
    await job_store.asave(job)
    await job_store.aset_status(job_id, JobStatus.FAILED, expected=JobStatus.RUNNING)


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


def _pop_frontier(job: Any) -> list[FrontierEntry]:
    normalize_job_state(job)
    max_pages = job_max_pages(job.policy, job.tier)
    if job.in_flight:
        fixed = set(job.visited) | {entry.url for entry in job.in_flight}
        if max_pages > 0 and len(fixed) > max_pages:
            record_cap_overcommitted(job)
        return list(job.in_flight[:BATCH_SIZE])
    capacity = remaining_page_capacity(len(job.visited), max_pages)
    if capacity is not None and capacity <= 0:
        if job.frontier:
            record_cap_truncated(job)
        del job.frontier[:]
        return []
    count = min(BATCH_SIZE, len(job.frontier))
    if capacity is not None:
        count = min(count, capacity)
    if capacity is not None and count == capacity and count < len(job.frontier):
        record_cap_truncated(job)
        del job.frontier[count:]
    batch = list(job.frontier[:count])
    del job.frontier[:count]
    return batch


def _job_engine_payload(job: Any, batch: Sequence[FrontierEntry]) -> dict[str, Any]:
    """Build one non-recursive engine batch from a whole-site job.

    The runner owns the durable frontier; the engine is a single-page fetcher per
    batch. Two owners of one property was the defect, so engine recursion is off.
    """
    # The engine's wire shape carries text in ``urls``; the entry's URL is an
    # admitted value by construction, so this projects identity, not validation.
    urls = [entry.url.raw for entry in batch]
    policy = read_policy(job.policy, job.tier)
    # Model_copy is safe because max_depth=0 is a declared value and this line
    # is ours, not a caller's: one engine batch is one page deep by definition.
    policy = policy.model_copy(update={"limits": policy.limits.model_copy(update={"max_depth": 0})})
    return {
        "urls": urls,
        "policy": policy,
        "url": urls[0],
        "headers": {},
        "request_profile": {},
    }


async def _pace_delayed_batch(
    context: JobAdmissionContext,
    batch: Sequence[FrontierEntry],
) -> None:
    tasks: list[CrawlTask] = []
    for index, entry in enumerate(batch):
        host = entry.url.host.lower()
        if host in context.delayed_hosts:
            tasks.append(
                CrawlTask(
                    index=index,
                    original_url=entry.url.raw,
                    normalized_url=entry.url,
                    domain=host,
                )
            )
    if not tasks:
        return

    def pace_result(job: CrawlTask) -> CrawlResult:
        # The status is derived from the label, not spelled beside it: two literals
        # go stale the day the taxonomy moves, and nothing in the suite sees it.
        classification: Classification = "rendered"
        return CrawlResult(
            url=job.normalized_url,
            attempt=terminal_attempt(
                strategy=None,
                status=status_for_classification(classification),
                classification=classification,
                elapsed_ms=0,
                error=None,
            ),
            final_url=job.normalized_url,
            html=None,
            text=None,
            attempts=0,
            domain=job.domain,
        )

    async def no_op(job: CrawlTask) -> CrawlResult:
        return pace_result(job)

    await context.scheduler.run(
        tasks,
        no_op,
        failed_result=lambda job, exc: pace_result(job),
    )


async def _seed_job_sitemaps(job: Any, context: JobAdmissionContext) -> None:
    candidates = await fetch_job_sitemap_candidates(job.root_url, context)
    outcome = await admit_job_links(job, candidates, source_depth=-1, context=context)
    job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + outcome.accepted


async def run_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
    fetch_and_decode: FetchAndDecode | None = None,
    *,
    preclaimed: bool = False,
) -> None:
    """Whole-site engine loop for one job; ``preclaimed`` says the caller's own
    PAUSED->RUNNING flip is the claim, so every other row is claimed by the CAS."""
    job = await job_store.aload(job_id)
    if job is None:
        return
    if not preclaimed:
        # The claim is the whole decision: a row that is not PENDING here belongs
        # to another process or to an operator who stopped it.
        if not await job_store.aset_status(job_id, JobStatus.RUNNING, expected=JobStatus.PENDING):
            return
    await job_store.asave(job)

    try:
        context = JobAdmissionContext.create(job.policy, job.tier, settings, fetch_and_decode)
        if context.max_depth > 0 and context.seed_sitemaps:
            await _seed_job_sitemaps(job, context)
            await job_store.asave(job)

        while True:
            job = await job_store.aload(job_id)
            if job is None:
                return
            if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
                return
            if shutdown_event.is_set():
                await job_store.aset_status(job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)
                return

            batch = _pop_frontier(job)
            if not batch:
                await job_store.asave(job)
                await job_store.aset_status(job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING)
                return
            if not job.in_flight:
                job.in_flight = list(batch)
            await job_store.asave(job)
            await _pace_delayed_batch(context, batch)

            result = await _execute_crawl(
                _job_engine_payload(job, batch),
                settings=settings,
                shutdown_event=shutdown_event,
            )
            items = result["results"]
            # Live state only: the runner is the job's single writer, and it
            # carries its own accumulators so the high-water mark stays consistent.
            reloaded = await job_store.areload_state(job_id)
            if reloaded is None:
                return
            job = replace(reloaded, results=job.results, visited=job.visited)

            # Two passes by priority: ``original_url`` (the caller's submission)
            # wins; redirect aliases only fill identities no result claimed.
            resolved: dict[NormalizedUrl, EngineCrawlResult] = {}
            for produced in items:
                admitted_original = admit_or_none(produced.original_url, policy=job.host_policy)
                if admitted_original is not None:
                    resolved.setdefault(admitted_original, produced)
            for produced in items:
                for key in (produced.final_url, produced.normalized_url):
                    if key is not None:
                        resolved.setdefault(key, produced)

            failed = 0
            for entry in batch:
                identity = entry.url
                job.in_flight = [pending for pending in job.in_flight if pending.url != identity]
                item = resolved.get(identity)
                if item is None:
                    outcome = "transient"
                elif item.status != "failed":
                    outcome = "success"
                elif is_transient_classification(item.classification):
                    outcome = "transient"
                else:
                    outcome = "terminal_failed"

                if (
                    outcome == "transient"
                    and job.transient_retries.get(identity, 0) + 1 < MAX_URL_TRANSIENT_ATTEMPTS
                ):
                    job.transient_retries[identity] = job.transient_retries.get(identity, 0) + 1
                    requeue_admitted_pages(
                        job,
                        (FrontierEntry(url=identity, depth=entry.depth),),
                        context,
                    )
                    continue

                job.transient_retries.pop(identity, None)
                job.visited.add(identity)
                if item is None:
                    # The batch's own shaping envelope names the unanswered URL, is
                    # reused rather than reinvented, and carries the admitted value.
                    job.results.append(failed_unified_result(identity).model_dump())
                    failed += 1
                    continue
                if outcome == "transient":
                    # The frozen dataclass's own substitute constructor: overridden
                    # fields are named and checked, never merged through a plain dict.
                    item = replace(item, status="failed", error="retries_exhausted")
                try:
                    unified = await abuild_unified_result(item)
                except Exception:
                    # Shaping is the batch's last line of defense and not meant to
                    # raise; if it does, the job still names this URL's result here.
                    unified = failed_unified_result(item)
                job.results.append(unified.model_dump())
                if item.status == "failed":
                    failed += 1
                admitted = await admit_job_links(
                    job,
                    item.discovered_links,
                    source_depth=entry.depth,
                    context=context,
                )
                job.stats["total_discovered"] = job.stats.get("total_discovered", 0) + admitted.accepted
            job.stats["total_crawled"] = len(job.visited)
            if failed:
                job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
            await job_store.asave(job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        await arecord_terminal_failure(job_store, job_id, str(exc))


async def _transition(
    job_store: JobStore,
    job_id: str,
    to: JobStatus,
    *,
    expected: JobStatus | frozenset[JobStatus],
) -> str:
    """Flip a job's status and say why if the flip did not happen.

    One three-way answer -- applied, missing, conflict -- produced once, not
    per control. Missing is told from conflict by re-reading the row."""
    if await job_store.aset_status(job_id, to, expected=expected):
        return "ok"
    return "missing" if await job_store.aload(job_id) is None else "conflict"


async def pause_job(job_id: str, job_store: JobStore) -> str:
    """Pause: flip the row; the task exits at the next batch boundary."""
    return await _transition(job_store, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING)


async def resume_job(job_id: str, job_store: JobStore, settings: Any, shutdown_event: asyncio.Event) -> str:
    """Resume a paused job by respawning its runner task."""
    outcome = await _transition(job_store, job_id, JobStatus.RUNNING, expected=JobStatus.PAUSED)
    if outcome == "ok":
        # The flip above is this process's claim, so the runner is told not to
        # take a second one -- the only way it can run without the CAS failing.
        start_job(job_id, job_store, settings, shutdown_event, preclaimed=True)
    return outcome


async def cancel_job(job_id: str, job_store: JobStore) -> str:
    """Cancel: flip the row; the task exits at the next batch boundary (the
    in-flight batch is still recorded — the engine already did the work)."""
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
