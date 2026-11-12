from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


class JobStatusResponse(BaseModel):
    """Status of a crawl job."""

    model_config = ConfigDict(extra="ignore")

    job_id: str
    root_url: str
    owner: Annotated[
        str,
        Field(
            description="The caller key id that owns this job; empty for every anonymous "
            "caller, past or present -- an anonymous POST /v1/jobs also writes an empty "
            "owner. A key-holder's id appears here, and is visible to any caller of "
            "GET /v1/jobs and the list endpoint, so treat it as a caller-visible label, "
            "not a secret.",
        ),
    ] = ""
    status: str
    kind: Annotated[
        str,
        Field(
            description="Job kind: whole_site resumes from its persisted frontier; single_shot "
            "does not. They differ in whether a crash leaves the job recoverable, so "
            "neither can be inferred from the other.",
        ),
    ]
    created_at: float
    updated_at: float
    frontier_size: int
    visited_size: int
    results_count: int
    cap_truncated: Annotated[
        bool,
        Field(
            description="True only when a positive job max_pages cap refused or truncated eligible queued or "
            "discovered pages. Ordinary BATCH_SIZE chunking does not set it; already-admitted in-flight "
            "overcommit is reported separately in stats.cap_overcommitted.",
        ),
    ] = False
    stats: dict[str, int] = Field(
        default_factory=dict,
        description="Job counters; cap_truncated marks eligible queued/discovered work refused by a positive "
        "cap, while cap_overcommitted marks already-admitted in-flight work preserved beyond that cap.",
    )
    error: str | None = None
