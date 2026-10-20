from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from app.models.job_status_response import JobStatusResponse


class JobListResponse(BaseModel):
    """A page of crawl jobs for the caller, with the caller's total row count."""

    model_config = ConfigDict(extra="ignore")

    jobs: list[JobStatusResponse]
    total: int = Field(description="Total jobs owned by the caller across all pages.")
