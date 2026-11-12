from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from crawlutron.vocabulary.limits import MAX_JOB_LIST_LIMIT


class JobListRequest(BaseModel):
    """Request body for the MCP ``list_jobs`` tool, mirroring GET /v1/jobs' query page."""

    model_config = ConfigDict(extra="forbid")

    limit: Annotated[
        int | None,
        Field(ge=1, le=MAX_JOB_LIST_LIMIT, description="Page size; the caller's default when omitted."),
    ] = None
    offset: Annotated[int, Field(ge=0, description="Rows to skip before this page.")] = 0
