"""Runner lease helpers for whole-site jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from crawlutron.storage.ids import new_handle_hex
from crawlutron.storage.job_store import JobStore

logger = logging.getLogger(__name__)


def new_claim_token(job_id: str) -> str:
    return f"{job_id}:{new_handle_hex()}"


async def is_claim_owner(job_store: JobStore, job_id: str, token: str) -> bool:
    """Whether this runner still owns the live RUNNING lease."""
    claim = await job_store.arunning_claim(job_id)
    return claim is not None and claim[0] == token and claim[2] == "running"


async def heartbeat_loop(job_store: JobStore, job_id: str, token: str, *, interval: float = 30.0) -> None:
    """Renew the runner lease while the task owns the RUNNING row."""
    try:
        while True:
            await asyncio.sleep(interval)
            if not await job_store.arenew_job_claim(job_id, token, now=time.time()):
                return
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await job_store.arelease_job_claim(job_id, token)
        raise
