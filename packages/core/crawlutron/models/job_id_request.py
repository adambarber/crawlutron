from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobIdRequest(BaseModel):
    """The one-job body both single-job MCP tools take: ``get_job`` and ``delete_job``."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="Identifier of the crawl job.")
