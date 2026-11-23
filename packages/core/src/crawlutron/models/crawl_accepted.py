"""The typed 202 accepted body of POST /v1/crawl, mirrored by the SDK."""

from __future__ import annotations

from pydantic import BaseModel


class CrawlAccepted(BaseModel):
    """The accepted body: status, request_id, status_url, and redacted webhook."""

    status: str
    request_id: str
    status_url: str
    webhook_url: str | None = None
