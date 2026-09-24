"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction and common response handling.
"""

import logging
import time
from typing import Any

from app.api_models import JobStatusResponse
from app.crawl.job_store import CrawlJobState
from app.metrics import get_metrics


def _build_job_status(job: CrawlJobState) -> JobStatusResponse:
    """Build a JobStatusResponse from a CrawlJobState."""
    return JobStatusResponse(
        job_id=job.job_id,
        root_url=job.root_url,
        status=job.status.value,
        created_at=job.created_at,
        updated_at=job.updated_at,
        frontier_size=len(job.frontier),
        visited_size=len(job.visited),
        results_count=len(job.results),
        stats=job.stats,
        error=job.error,
    )


def record_crawl_completion(
    result: dict[str, Any],
    started: float,
    *,
    event: str,
    logger: logging.Logger,
) -> None:
    results = result.get("results", [])
    get_metrics().observe_crawl(
        results=results,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    logger.info(event, extra={"url_count": len(results)})


__all__ = ["_build_job_status", "record_crawl_completion"]
