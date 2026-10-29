"""Startup and crash job recovery: retire orphans, resume whole-site rows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

from app.api.job_lifecycle import is_running, start_tracked_task
from app.config.settings import CrawlerSettings
from app.storage.job_store import JobKind, JobStatus, JobStore

# The spawn seam is injected so this module never imports the runner; it
# spawns a job the way ``app.api.job_runner.start_job`` does.
type _StartRunner = Callable[..., "asyncio.Task"]

_DRAIN_TASK_ID = "_recovery_backlog_drain"

_logger = logging.getLogger(__name__)


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
) -> int:
    """Lifespan startup: retire crash orphans, then run every PENDING job.

    ``start_runner`` is injected by the caller (usually ``job_runner.start_job``)
    so recovery never imports the runner; the bound is the runner-cap rule.
    """
    await recover_orphaned_running_jobs(job_store)
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
