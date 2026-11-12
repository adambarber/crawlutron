from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from crawlutron.models.job_control_request import normalize_job_action


class JobControlToolRequest(BaseModel):
    """The MCP ``control_job`` body: REST's path parameter and body in one shape."""

    model_config = ConfigDict(extra="forbid")

    job_id: str = Field(..., min_length=1, description="Identifier of the crawl job to control.")
    action: Annotated[
        Literal["pause", "resume", "cancel"],
        BeforeValidator(normalize_job_action),
        Field(description="One of: pause, resume, cancel."),
    ]
