from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="forbid")

    action: Literal["pause", "resume", "cancel"] = Field(..., description="One of: pause, resume, cancel.")
