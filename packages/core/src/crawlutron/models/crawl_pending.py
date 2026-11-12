"""The typed pending state of a stored-crawl read."""

from __future__ import annotations


class CrawlPendingError(Exception):
    """A stored-crawl read raced a job that is still running.

    Typed, deliberately distinct from ``HTTPException``: REST converts it to a
    202; the MCP read tools answer it as a structured non-error result.
    """

    def __init__(self, request_id: str, status: str) -> None:
        super().__init__(f"Job {request_id} is still {status}")
        self.request_id = request_id
        self.job_status: str = status
