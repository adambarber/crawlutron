"""The ``GET /v1/crawl/{request_id}/trace`` body."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.page_trace import PageTrace


class TraceResponse(BaseModel):
    """The stored crawl response read as a trace.

    Two keys of the stored envelope plus the pages' diagnostic spines, in
    submitted order, validated straight off that envelope's dump (no translation layer).
    """

    request_id: str = Field(description="Request identifier the trace was recorded under.")
    results: list[PageTrace] = Field(description="One trace per submitted URL, in submitted order.")
