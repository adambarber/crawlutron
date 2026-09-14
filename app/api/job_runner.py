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
from collections.abc import Awaitable, Callable
from typing import Any

from app.api.crawl_executor import execute_crawl as _real_execute_crawl
from app.api.response_shaping import build_unified_result, frontier_link_url
from app.crawl.job_store import JobKind, JobStatus, JobStore

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
            job = job_store.load(summary["job_id"])
            if job is not None:
                job.status = JobStatus.PENDING
                job_store.save(job)
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


def _pop_frontier(job: Any) -> list[str]:
    """Pop up to BATCH_SIZE urls off the persisted frontier (in order)."""
    batch = job.frontier[:BATCH_SIZE]
    del job.frontier[: len(batch)]
    known = job.__dict__.get("_frontier_set")
    if known is not None:
        for url in batch:
            known.discard(url)
    return batch


def _frontier_set(job: Any) -> set[str]:
    """O(1) membership mirror of ``job.frontier`` for dedup checks (M2).

    The persisted frontier stays a plain list (JSON-serializable, crash-safe);
    the set is transient — rebuilt on first use after every store load — and
    self-heals if its size ever disagrees with the list. All mutations flow
    through ``_pop_frontier``/``_frontier_add`` so the mirror is exact.
    """
    known = job.__dict__.get("_frontier_set")
    if known is None or len(known) != len(job.frontier):
        known = set(job.frontier)
        job.__dict__["_frontier_set"] = known
    return known


def _frontier_add(job: Any, url: str) -> None:
    """Append to the persisted frontier and its membership mirror."""
    job.frontier.append(url)
    known = job.__dict__.get("_frontier_set")
    if known is not None:
        known.add(url)


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
    if job.in_flight:
        # Re-queue a batch that was popped (write-ahead) but never recorded because
        # the process died inside the engine call. At-least-once: visited + the
        # content cache make the re-fetch cheap and non-duplicating, so a crash
        # resumes instead of silently completing with the batch lost.
        for url in job.in_flight:
            if url not in job.visited and url not in _frontier_set(job):
                _frontier_add(job, url)
        job.in_flight = []
    job.status = JobStatus.RUNNING
    job_store.save(job)

    try:
        while True:
            job = job_store.load(job_id)
            if job is None:
                return  # deleted mid-run; row is gone, nothing to update
            if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
                return  # control transition already persisted by handler
            if shutdown_event.is_set():
                job.status = JobStatus.PAUSED
                job_store.save(job)
                return

            batch = _pop_frontier(job)
            if not batch:
                job.in_flight = []  # clean terminal state even if a crash left it set
                job.status = JobStatus.COMPLETED
                job_store.save(job)
                return
            # Write-ahead the pop: the post-engine reload must see the
            # shrunk frontier or the loop refetches the same batch forever.
            # Record the batch as in_flight in the same save so a crash before
            # the results commit re-queues exactly these urls (at-least-once).
            job.in_flight = list(batch)
            job_store.save(job)

            payload: dict[str, Any] = {
                "urls": batch,
                "policy": dict(job.policy or {}),
                "url": batch[0],
                "headers": {},
                "request_profile": {},
            }
            result = await _execute_crawl(payload, settings=settings, shutdown_event=shutdown_event)
            items = result.get("results", [])

            # The engine call has completed — its work must be recorded even
            # if a control transition lands mid-batch: results are real,
            # discovered links are real, and the frontier already reflects
            # the write-ahead pop. Record, then honor the transition below.
            job = job_store.load(job_id)
            if job is None:
                return

            failed = 0
            for item in items:
                url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
                if not url:
                    continue
                try:
                    unified = build_unified_result(item)
                except Exception:
                    unified = None
                if unified is not None:
                    job.results.append(unified.model_dump())
                    if item.get("status") == "failed":
                        failed += 1
                else:
                    failed += 1  # a dropped item is a failed crawl, not silence
                job.visited.add(url)
                job.stats["total_crawled"] = len(job.visited)
                for link in item.get("discovered_links") or []:
                    link_url = frontier_link_url(link)
                    if not link_url:
                        continue
                    if link_url not in job.visited and link_url not in _frontier_set(job):
                        _frontier_add(job, link_url)
                        job.stats["total_discovered"] += 1
            # `failed` accumulates across the batch: merge it once, outside
            # the loop, or every item re-adds the running total (1+2+...+n).
            if failed:
                job.stats["total_failed"] = job.stats.get("total_failed", 0) + failed
            job.in_flight = []  # batch committed; a later resume must not re-crawl it
            job_store.save(job)

            if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
                # Transition landed mid-batch: state is already persisted
                # with everything the engine produced; stop the loop here.
                return
    except asyncio.CancelledError:
        # delete()/shutdown cancellation: leave any pre-cancel state intact.
        raise
    except Exception as exc:
        logger.exception("job_runner_failed job_id=%s error=%r", job_id, exc)
        job = job_store.load(job_id)
        if job is not None and job.status == JobStatus.RUNNING:
            job.status = JobStatus.FAILED
            job.error = str(exc)
            job_store.save(job)


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
    job = job_store.load(job_id)
    if job is None:
        return "missing"
    if job.status != JobStatus.RUNNING:
        return "conflict"
    job.status = JobStatus.PAUSED
    job_store.save(job)
    return "ok"


async def resume_job(job_id: str, job_store: JobStore, settings: Any, shutdown_event: asyncio.Event) -> str:
    """Resume a paused job by respawning its runner task."""
    job = job_store.load(job_id)
    if job is None:
        return "missing"
    if job.status != JobStatus.PAUSED:
        return "conflict"
    job.status = JobStatus.RUNNING
    job_store.save(job)
    start_job(job_id, job_store, settings, shutdown_event)
    return "ok"


async def cancel_job(job_id: str, job_store: JobStore) -> str:
    """Cancel: flip the row; the task exits at the next batch boundary (the
    in-flight batch is still recorded — the engine already did the work)."""
    job = job_store.load(job_id)
    if job is None:
        return "missing"
    if job.status not in {JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.PENDING}:
        return "conflict"
    job.status = JobStatus.CANCELLED
    job_store.save(job)
    return "ok"


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
