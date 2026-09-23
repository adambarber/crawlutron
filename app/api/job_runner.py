"""Background whole-site job runner for ``/v1/jobs`` (H5 make-it-work).

Consumes the persisted frontier of a whole-site job in persistent batches:
every transition (pause, resume, cancel, crash) leaves the store in a
state that can be resumed later. Batches call the shared
:func:`app.api.crawl_executor.execute_crawl` (seam: ``install_execute_crawl``
for tests), so jobs run through the exact same engine as ``/v1/crawl``.

Design notes
------------
* One :class:`asyncio.Task` per running job, tracked in a module registry.
* ``pause`` flips the store row to PAUSED; the task sees it between batches
  and exits without touching the row (frontier/visited retained).
* ``cancel`` flips to CANCELLED; the task also drops the in-memory task
  handle. Visited/frontier state is retained for auditability.
* ``delete`` on a running job cancels its task before removing the row.
* Crash recovery: :func:`recover_orphaned_running_jobs` rewinds RUNNING
  rows to PENDING at startup; lifespan then auto-starts PENDING jobs.

The registry accessors are async-safe: control transitions happen in the
request handlers (store writes are lock-guarded in JobStore), while the
runner task only transitions between batches.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any
from urllib.parse import urlparse

from app.api.crawl_executor import execute_crawl as _real_execute_crawl
from app.api.response_shaping import abuild_unified_result
from app.constants import MAX_URL_TRANSIENT_ATTEMPTS
from app.crawl.admission import (
    JobAdmissionContext,
    admit_job_links,
    canonical_page_identity,
    fetch_job_sitemap_candidates,
    job_max_pages,
    normalize_job_state,
    normalized_policy,
    policy_limits,
    record_cap_overcommitted,
    record_cap_truncated,
    remaining_page_capacity,
    requeue_admitted_pages,
)
from app.crawl.job_store import FrontierEntry, JobKind, JobStatus, JobStore
from app.crawl.strategies.browser import TRANSIENT_CLASSIFICATIONS
from app.models import CrawlResult, CrawlTask

logger = logging.getLogger(__name__)

# Frontier batch size per engine call. Bounds per-call result memory the
# same way the webhook runner bounds whole-crawl memory.
BATCH_SIZE = 8

ExecuteCrawl = Callable[..., Awaitable[dict[str, Any]]]
_execute_crawl: ExecuteCrawl = _real_execute_crawl


def install_execute_crawl(fn: ExecuteCrawl) -> None:
    """Swap the engine seam (tests only)."""
    global _execute_crawl
    _execute_crawl = fn


def reset_execute_crawl() -> None:
    global _execute_crawl
    _execute_crawl = _real_execute_crawl


_tasks: dict[str, asyncio.Task] = {}


def is_running(job_id: str) -> bool:
    task = _tasks.get(job_id)
    return task is not None and not task.done()


async def shutdown_job_runner() -> None:
    """Cancel all running job tasks and await their exit."""
    for task in list(_tasks.values()):
        task.cancel()
    for job_id, task in list(_tasks.items()):
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.warning("job_runner_shutdown_task_error", extra={"job_id": job_id})
    _tasks.clear()


def reset_job_runner() -> None:
    """Best-effort synchronous teardown for test fixtures (no await needed)."""
    for task in list(_tasks.values()):
        task.cancel()
    _tasks.clear()
    reset_execute_crawl()


def recover_orphaned_running_jobs(job_store: JobStore) -> int:
    """Rewind RUNNING rows to PENDING (crash recovery). Returns count."""
    recovered = 0
    for summary in job_store.list_jobs():
        if summary.get("status") == JobStatus.RUNNING.value:
            # Control-plane CAS: rewind RUNNING -> PENDING without rewriting
            # the row's data (H3: save() no longer carries status anyway).
            if job_store.set_status(
                summary["job_id"], JobStatus.PENDING, expected=JobStatus.RUNNING
            ):
                recovered += 1
    return recovered


async def recover_and_start_pending_jobs(
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> int:
    """Lifespan startup: rewind crash orphans, then run every PENDING job.

    Whole-site jobs resume from their persisted frontier; returns the number
    of runner tasks started.
    """
    recover_orphaned_running_jobs(job_store)
    started = 0
    for summary in job_store.list_jobs():
        if summary.get("status") != JobStatus.PENDING.value:
            continue
        if summary.get("kind") not in (None, JobKind.WHOLE_SITE.value):
            continue
        job = job_store.load(summary["job_id"])
        if job is None:
            continue
        start_job(job.job_id, job_store, settings, shutdown_event)
        started += 1
    return started


def _pop_frontier(job: Any) -> list[FrontierEntry]:
    normalize_job_state(job)
    max_pages = job_max_pages(job.policy)
    if job.in_flight:
        fixed = {
            canonical_page_identity(url, host_policy=job.host_policy) or url
            for url in set(job.visited) | {entry.url for entry in job.in_flight}
        }
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
    urls = [entry.url for entry in batch]
    policy = normalized_policy(job.policy)
    limits = policy_limits(policy)
    limits["max_depth"] = 0
    policy["limits"] = limits
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
        host = (urlparse(entry.url).hostname or "").lower()
        identity = canonical_page_identity(entry.url, host_policy=context.host_policy)
        if host in context.delayed_hosts and identity is not None:
            tasks.append(
                CrawlTask(
                    index=index,
                    original_url=entry.url,
                    normalized_url=identity,
                    domain=host,
                )
            )
    if not tasks:
        return

    def pace_result(job: CrawlTask) -> CrawlResult:
        return CrawlResult(
            url=job.original_url,
            status="success",
            classification="rendered",
            final_url=job.original_url,
            html=None,
            text=None,
            attempts=0,
            elapsed_ms=0,
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


async def _run_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> None:
    """Whole-site engine loop for one job. Runs until terminal/paused/cancel."""
    job = job_store.load(job_id)
    if job is None:
        return
    claimed = await asyncio.to_thread(
        job_store.set_status, job_id, JobStatus.RUNNING, expected=JobStatus.PENDING
    )
    if not claimed and job.status is not JobStatus.RUNNING:
        return
    await asyncio.to_thread(job_store.save, job)

    try:
        context = JobAdmissionContext.create(job.policy, settings)
        if context.max_depth > 0 and context.seed_sitemaps:
            await _seed_job_sitemaps(job, context)
            await asyncio.to_thread(job_store.save, job)

        while True:
            job = job_store.load(job_id)
            if job is None:
                return
            if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
                return
            if shutdown_event.is_set():
                await asyncio.to_thread(
                    job_store.set_status, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING
                )
                return

            batch = _pop_frontier(job)
            if not batch:
                await asyncio.to_thread(job_store.save, job)
                await asyncio.to_thread(
                    job_store.set_status, job_id, JobStatus.COMPLETED, expected=JobStatus.RUNNING
                )
                return
            if not job.in_flight:
                job.in_flight = list(batch)
            await asyncio.to_thread(job_store.save, job)
            await _pace_delayed_batch(context, batch)

            result = await _execute_crawl(
                _job_engine_payload(job, batch),
                settings=settings,
                shutdown_event=shutdown_event,
            )
            items = result.get("results", [])
            job = job_store.load(job_id)
            if job is None:
                return

            # Index results by every URL they are known by, in TWO passes with
            # different priority. A result's own `original_url` is what the
            # caller submitted and is therefore authoritative for that entry;
            # redirect aliases (final_url/normalized_url) only fill identities
            # no result claimed for itself. A single first-writer-wins pass made
            # a redirect steal another entry's slot: with batch [a, b] where `a`
            # redirects to `b/`, the redirect result claimed b's identity first
            # and b's real result was never recorded.
            resolved: dict[str, dict[str, Any]] = {}
            for item in items:
                original = item.get("original_url")
                if isinstance(original, str) and original:
                    resolved.setdefault(
                        canonical_page_identity(original, host_policy=context.host_policy) or original,
                        item,
                    )
            for item in items:
                for key in (item.get("final_url"), item.get("normalized_url")):
                    if not isinstance(key, str) or not key:
                        continue
                    identity = canonical_page_identity(key, host_policy=context.host_policy) or key
                    resolved.setdefault(identity, item)

            failed = 0
            for entry in batch:
                url = entry.url
                identity = canonical_page_identity(url, host_policy=context.host_policy) or url
                job.in_flight = [
                    pending
                    for pending in job.in_flight
                    if (
                        canonical_page_identity(pending.url, host_policy=context.host_policy)
                        or pending.url
                    ) != identity
                ]
                item = resolved.get(identity)
                if item is None:
                    outcome = "transient"
                elif item.get("status") != "failed":
                    outcome = "success"
                elif item.get("classification") in TRANSIENT_CLASSIFICATIONS:
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
                    failed += 1
                    continue
                if outcome == "transient":
                    item = {**item, "status": "failed", "error": "retries_exhausted"}
                try:
                    unified = await abuild_unified_result(item)
                except Exception:
                    unified = None
                if unified is not None:
                    job.results.append(unified.model_dump())
                    if item.get("status") == "failed":
                        failed += 1
                else:
                    failed += 1
                admitted = await admit_job_links(
                    job,
                    item.get("discovered_links") or [],
                    source_depth=entry.depth,
                    context=context,
                )
                job.stats["total_discovered"] = (
                    job.stats.get("total_discovered", 0) + admitted.accepted
                )
            job.stats["total_crawled"] = len(job.visited)
            if failed:
                job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
            await asyncio.to_thread(job_store.save, job)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        job = job_store.load(job_id)
        if job is not None and job.status == JobStatus.RUNNING:
            job.error = str(exc)
            await asyncio.to_thread(job_store.save, job)
            await asyncio.to_thread(
                job_store.set_status, job_id, JobStatus.FAILED, expected=JobStatus.RUNNING
            )



def start_job(
    job_id: str,
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> asyncio.Task:
    """Spawn (or return) the runner task for a job."""
    existing = _tasks.get(job_id)
    if existing is not None and not existing.done():
        return existing
    task = asyncio.create_task(_run_job(job_id, job_store, settings, shutdown_event))
    _tasks[job_id] = task
    task.add_done_callback(lambda t: _tasks.pop(job_id, None))
    return task


def cancel_job_task(job_id: str) -> None:
    """Cancel a running job task (delete path); state row untouched."""
    task = _tasks.get(job_id)
    if task is not None and not task.done():
        task.cancel()


async def pause_job(job_id: str, job_store: JobStore) -> str:
    """Pause: flip the row; the task exits at the next batch boundary."""
    ok = await asyncio.to_thread(
        job_store.set_status, job_id, JobStatus.PAUSED, expected=JobStatus.RUNNING
    )
    if ok:
        return "ok"
    return "missing" if job_store.load(job_id) is None else "conflict"


async def resume_job(job_id: str, job_store: JobStore, settings: Any, shutdown_event: asyncio.Event) -> str:
    """Resume a paused job by respawning its runner task."""
    ok = await asyncio.to_thread(
        job_store.set_status, job_id, JobStatus.RUNNING, expected=JobStatus.PAUSED
    )
    if not ok:
        return "missing" if job_store.load(job_id) is None else "conflict"
    start_job(job_id, job_store, settings, shutdown_event)
    return "ok"


async def cancel_job(job_id: str, job_store: JobStore) -> str:
    """Cancel: flip the row; the task exits at the next batch boundary (the
    in-flight batch is still recorded — the engine already did the work)."""
    ok = await asyncio.to_thread(
        job_store.set_status,
        job_id,
        JobStatus.CANCELLED,
        expected=frozenset({JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PENDING}),
    )
    if ok:
        return "ok"
    return "missing" if job_store.load(job_id) is None else "conflict"


__all__ = [
    "BATCH_SIZE",
    "cancel_job",
    "cancel_job_task",
    "install_execute_crawl",
    "is_running",
    "pause_job",
    "recover_orphaned_running_jobs",
    "reset_job_runner",
    "resume_job",
    "shutdown_job_runner",
    "start_job",
]
