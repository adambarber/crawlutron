"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction and common error handling
to eliminate code duplication.
"""

from app.api_models import JobStatusResponse
from app.crawl.job_store import CrawlJobState
from fastapi import HTTPException


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


def _raise_not_found(job_id: str) -> None:
    """Raise HTTP 404 for job not found."""
    raise HTTPException(
        status_code=404,
        detail={"code": "not_found", "message": "Job not found", "details": {"job_id": job_id}},
    )
