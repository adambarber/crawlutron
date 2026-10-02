from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobCreateResponse(BaseModel):
    """Response from creating a crawl job."""

    model_config = ConfigDict(extra="ignore")

    job_id: str = Field(..., description="Unique job identifier.")
    status: str = Field(..., description="Initial job status (always 'pending').")
    root_url: str
    created_at: float
