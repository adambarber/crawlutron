"""Startup and crash job recovery: retire orphans, resume whole-site rows."""

from __future__ import annotations

import asyncio

from app.api.job_lifecycle import is_running, start_job, start_tracked_task
from app.config.settings import CrawlerSettings
from app.storage.job_store import JobKind, JobStatus, JobStore

_DRAIN_TASK_ID = "_recovery_backlog_drain"


def _is_resumable_kind(kind: str) -> bool:
    """Whether a job of this kind can be resumed from its persisted frontier.

    One definition for both recovery seams; ``kind`` is never absent (the
    SQL coalesces the column to ``'whole_site'``)."""
    return kind == JobKind.WHOLE_SITE.value


async def recover_orphaned_running_jobs(job_store: JobStore) -> int:
    """Retire crash orphans: rewind the resumable, fail the rest.

    Async: the lifespan calls it with a live loop, and it reads every job row.
    """
    recovered = 0
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.RUNNING.value:
            continue
        # A RUNNING job this process still drives is owned, not orphaned; boot
        # time recovery must not rewind another live worker's in-flight work.
        if is_running(summary["job_id"]):
            continue
        # A job that cannot be resumed gets its terminal status here, while
        # this is the only place that knows the service gave up on it.
        retired = JobStatus.PENDING if _is_resumable_kind(summary["kind"]) else JobStatus.FAILED
        # Control-plane CAS: retires the row without rewriting its data;
        # save() no longer carries status anyway.
        if await job_store.aset_status(summary["job_id"], retired, expected=JobStatus.RUNNING):
            recovered += 1
    return recovered


def recovery_start_limit(settings: CrawlerSettings) -> int:
    """How many runners boot recovery may hold at once.

    Sized like the create path's global concurrency: one runner per global
    slot, floored at 1 (#523 COR-09)."""
    return max(1, int(getattr(settings, "global_concurrency", 4)))


async def _drain_backlog(
    backlog: list[str],
    live: set[asyncio.Task],
    limit: int,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
) -> None:
    """Start queued backlog rows as recovery slots free, never above ``limit``."""
    for job_id in backlog:
        if len(live) >= limit:
            done, live = await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED)
            live -= done
        live.add(start_job(job_id, job_store, settings, shutdown_event))
    if live:
        # Hold the last slots until their runners exit so the drain never
        # outlives the work it queued.
        await asyncio.wait(live)


async def recover_and_start_pending_jobs(
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
) -> int:
    """Lifespan startup: retire crash orphans, then run every PENDING job.

    Bounded by recovery_start_limit (#523 COR-09); backlog rows queue behind a drain task."""
    await recover_orphaned_running_jobs(job_store)
    limit = recovery_start_limit(settings)
    started = 0
    live: set[asyncio.Task] = set()
    backlog: list[str] = []
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.PENDING.value:
            continue
        # #461: a PENDING row of an unresumable kind can never be picked up by
        # anything else -- retire it terminal instead of leaving it pending forever.
        if not _is_resumable_kind(summary["kind"]):
            # #461: retire terminal via CAS; a lost CAS means it moved meanwhile.
            await job_store.aset_status(summary["job_id"], JobStatus.FAILED, expected=JobStatus.PENDING)
            continue
        if await job_store.aload(summary["job_id"]) is None:
            continue
        if len(live) >= limit:
            backlog.append(summary["job_id"])
            continue
        live.add(start_job(summary["job_id"], job_store, settings, shutdown_event))
        started += 1
    if backlog:
        # #523 COR-09: queued rows wait for a slot instead of each spawning a
        # runner at boot; the drain rides the #448 registry so shutdown drains it.
        start_tracked_task(
            _DRAIN_TASK_ID,
            lambda: _drain_backlog(backlog, live, limit, job_store, settings, shutdown_event),
        )
        started += len(backlog)
    return started
