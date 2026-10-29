from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="forbid")

    action: str = Field(..., description="One of: pause, resume, cancel.")
