"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction and common error handling
to eliminate code duplication.
"""

from app.api_models import JobStatusResponse
from app.crawl.job_store import CrawlJobState


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


__all__ = ["_build_job_status"]
