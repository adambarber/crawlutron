"""The ``GET /v1/crawl/{request_id}/trace`` body."""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.models.page_trace import PageTrace


class TraceResponse(BaseModel):
    """The stored crawl response read as a trace.

    Two keys of the stored envelope and the pages' diagnostic spines, in
    submitted order. It validates straight off that envelope's own dump, so
    there is no translation layer between the record and the view of it.
    """

    request_id: str = Field(description="Request identifier the trace was recorded under.")
    results: list[PageTrace] = Field(description="One trace per submitted URL, in submitted order.")
