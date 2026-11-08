"""Job control request body."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

_ALLOWED_ACTIONS = ("pause", "resume", "cancel")


def normalize_job_action(value: object) -> object:
    return value.lower() if isinstance(value, str) else value


class JobControlRequest(BaseModel):
    """Control a crawl job (pause/resume/cancel)."""

    model_config = ConfigDict(extra="forbid")

    action: Annotated[
        Literal["pause", "resume", "cancel"],
        BeforeValidator(normalize_job_action),
        Field(description="One of: pause, resume, cancel."),
    ]
