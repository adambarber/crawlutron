"""Startup and crash job recovery: retire orphans, resume whole-site rows."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable

from crawlutron.config.settings import CrawlerSettings
from crawlutron.service.job_lifecycle import start_tracked_task
from crawlutron.storage.job_store import DEFAULT_JOB_CLAIM_SECONDS, JobKind, JobStatus, JobStore
from crawlutron.storage.safe_db import StorageError

# The spawn seam is injected so this module never imports the runner; it
# spawns a job the way ``crawlutron.service.job_runner.start_job`` does.
type _StartRunner = Callable[..., "asyncio.Task"]

_DRAIN_TASK_ID = "_recovery_backlog_drain"

_logger = logging.getLogger(__name__)


def _is_resumable_kind(kind: str) -> bool:
    """Whether a job of this kind can be resumed from its persisted frontier.

    One definition for both recovery seams; ``kind`` is never absent (the
    SQL coalesces the column to ``'whole_site'``)."""
    return kind == JobKind.WHOLE_SITE.value


async def recover_orphaned_running_jobs(
    job_store: JobStore,
    *,
    now: float | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> int:
    """Retire crash orphans: rewind the resumable, fail the rest.

    A RUNNING row with a fresh lease is a live foreign worker and is left alone.
    Only unclaimed or stale-claimed rows are retired.
    """
    recovered = 0
    at = time.time() if now is None else now
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.RUNNING.value:
            continue
        if await job_store.ais_job_claim_fresh(summary["job_id"], now=at, claim_seconds=claim_seconds):
            continue
        retired = JobStatus.PENDING if _is_resumable_kind(summary["kind"]) else JobStatus.FAILED
        if await job_store.aretire_orphan_running(
            summary["job_id"], retired, now=at, claim_seconds=claim_seconds
        ):
            recovered += 1
    return recovered


def recovery_start_limit(settings: CrawlerSettings) -> int:
    """Boot recovery's runner cap: one runner per global slot."""
    return max(1, int(settings.global_concurrency))


async def _drain_backlog(
    backlog: list[str],
    live: set[asyncio.Task],
    limit: int,
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    start_runner: _StartRunner,
) -> None:
    """Start queued backlog rows as recovery slots free, never above ``limit``."""
    for job_id in backlog:
        if len(live) >= limit:
            done, live = await asyncio.wait(live, return_when=asyncio.FIRST_COMPLETED)
            live -= done
        live.add(start_runner(job_id, job_store, settings, shutdown_event))
    if live:
        # Hold the last slots until their runners exit so the drain never
        # outlives the work it queued.
        await asyncio.wait(live)


async def recover_and_start_pending_jobs(
    job_store: JobStore,
    settings: CrawlerSettings,
    shutdown_event: asyncio.Event,
    start_runner: _StartRunner,
    *,
    now: float | None = None,
    claim_seconds: float = DEFAULT_JOB_CLAIM_SECONDS,
) -> int:
    """Lifespan startup: retire crash orphans, then run every PENDING job.

    ``start_runner`` is injected by the caller (usually ``job_runner.start_job``)
    so recovery never imports the runner; the bound is the runner-cap rule.
    """
    await recover_orphaned_running_jobs(job_store, now=now, claim_seconds=claim_seconds)
    limit = recovery_start_limit(settings)
    started = 0
    live: set[asyncio.Task] = set()
    backlog: list[str] = []
    for summary in await job_store.alist_jobs():
        if summary.get("status") != JobStatus.PENDING.value:
            continue
        # A PENDING row of an unresumable kind can never be picked up by
        # anything else -- retire it terminal instead of leaving it pending forever.
        if not _is_resumable_kind(summary["kind"]):
            # Retire terminal via CAS; a lost CAS means it moved meanwhile.
            claimed = await job_store.aset_status(
                summary["job_id"], JobStatus.FAILED, expected=JobStatus.PENDING
            )
            if not claimed:
                _logger.warning(
                    "recovery_cas_lost",
                    extra={"job_id": summary["job_id"]},
                )
            continue
        if await job_store.aload(summary["job_id"]) is None:
            continue
        if len(live) >= limit:
            backlog.append(summary["job_id"])
            continue
        live.add(start_runner(summary["job_id"], job_store, settings, shutdown_event))
        started += 1
    if backlog:
        # Queued rows wait for a slot instead of each spawning a
        # runner at boot; the drain rides the task registry so shutdown drains it.
        start_tracked_task(
            _DRAIN_TASK_ID,
            lambda: _drain_backlog(backlog, live, limit, job_store, settings, shutdown_event, start_runner),
        )
        started += len(backlog)
    return started


async def pause_for_recovery(job_store: JobStore, job_id: str, token: str, error: str | None) -> None:
    """Transition RUNNING to PAUSED so recovery resumes the job (claim preserved).
    Fail-safe: a StorageError from aset_status(PAUSED) leaves the row RUNNING
    with a cleared claim so boot recovery rewinds it -- never a new FAILED path."""
    try:
        await job_store.aset_status(
            job_id,
            JobStatus.PAUSED,
            expected=JobStatus.RUNNING,
            error=error,
        )
        return
    except StorageError as exc:
        _logger.exception("job_runner_pause_failed job_id=%s error=%r", job_id, exc)
    try:
        await job_store.aclear_job_claim(job_id)
    except StorageError:
        _logger.exception("job_runner_claim_clear_failed job_id=%s", job_id)
