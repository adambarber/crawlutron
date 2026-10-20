"""Startup and crash job recovery: retire orphans, resume whole-site rows."""

from __future__ import annotations

import asyncio
from typing import Any

from app.api.job_lifecycle import is_running, start_job
from app.storage.job_store import JobKind, JobStatus, JobStore


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


async def recover_and_start_pending_jobs(
    job_store: JobStore,
    settings: Any,
    shutdown_event: asyncio.Event,
) -> int:
    """Lifespan startup: retire crash orphans, then run every PENDING job.

    Whole-site jobs resume from their persisted frontier."""
    await recover_orphaned_running_jobs(job_store)
    started = 0
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.PENDING.value:
            continue
        if not _is_resumable_kind(summary["kind"]):
            continue
        job = await job_store.aload(summary["job_id"])
        if job is None:
            continue
        start_job(job.job_id, job_store, settings, shutdown_event)
        started += 1
    return started
