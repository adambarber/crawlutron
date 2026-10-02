from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from app.models.job_status_response import JobStatusResponse


class JobListResponse(BaseModel):
    """List of crawl jobs."""

    model_config = ConfigDict(extra="ignore")

    jobs: list[JobStatusResponse]
