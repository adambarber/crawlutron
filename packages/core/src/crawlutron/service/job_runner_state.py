"""Job state transitions shared by the runner's startup, loop and completion paths."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Sequence
from dataclasses import replace

from crawlutron.storage.job_store import CrawlJobState, FrontierEntry, JobStatus, JobStore


async def load_and_claim(
    job_id: str, job_store: JobStore, *, preclaimed: bool, claim_token: str, claim_seconds: float
) -> CrawlJobState | None:
    job = await job_store.aload(job_id)
    if job is None:
        return None
    now = time.time()
    if preclaimed:
        if not await job_store.arenew_job_claim(job_id, claim_token, now=now):
            return None
    elif not await job_store.aset_status(
        job_id,
        JobStatus.RUNNING,
        expected=JobStatus.PENDING,
        claimed_by=claim_token,
        claim_now=now,
        claim_seconds=claim_seconds,
    ):
        return None
    return replace(job, status=JobStatus.RUNNING, claimed_by=claim_token, claimed_at=now)


async def should_stop(
    job: CrawlJobState, job_id: str, job_store: JobStore, shutdown_event: asyncio.Event, *, claim_token: str
) -> bool:
    if job.status in {JobStatus.PAUSED, JobStatus.CANCELLED}:
        return True
    if shutdown_event.is_set():
        await job_store.asave(job)
        await job_store.aset_status(
            job_id,
            JobStatus.PAUSED,
            expected=JobStatus.RUNNING,
            claimed_by=claim_token,
            require_owner=True,
            clear_claim=True,
        )
        return True
    return False


async def mark_complete_if_empty(
    job: CrawlJobState, job_id: str, job_store: JobStore, batch: Sequence[FrontierEntry], *, claim_token: str
) -> bool:
    if batch:
        return False
    await job_store.asave(job)
    await job_store.aset_status(
        job_id,
        JobStatus.COMPLETED,
        expected=JobStatus.RUNNING,
        claimed_by=claim_token,
        require_owner=True,
        clear_claim=True,
    )
    return True
