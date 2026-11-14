"""Runner lease helpers for whole-site jobs."""

from __future__ import annotations

import logging

from crawlutron.storage.ids import new_handle_hex
from crawlutron.storage.job_store import JobStore

logger = logging.getLogger(__name__)


def new_claim_token(job_id: str) -> str:
    return f"{job_id}:{new_handle_hex()}"


async def is_claim_owner(job_store: JobStore, job_id: str, token: str) -> bool:
    """Whether this runner still owns the live RUNNING lease."""
    claim = await job_store.arunning_claim(job_id)
    return claim is not None and claim[0] == token and claim[2] == "running"
