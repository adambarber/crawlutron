"""Runner lease helpers for whole-site jobs."""

from __future__ import annotations

import logging

from crawlutron.storage.ids import new_handle_hex

logger = logging.getLogger(__name__)


def new_claim_token(job_id: str) -> str:
    return f"{job_id}:{new_handle_hex()}"
