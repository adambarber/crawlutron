"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction and common response handling.
"""

import logging
import time
from typing import Any

from app.api_models import JobStatusResponse
from app.metrics import get_metrics


def _build_job_status(row: dict[str, Any]) -> JobStatusResponse:
    """Build a JobStatusResponse from a store status view.

    Takes the view rather than a ``CrawlJobState`` so the counts come from the
    store's projection instead of ``len()`` over the page history: that was
    what made polling a job's status cost as much as reading its whole crawl.
    """
    return JobStatusResponse(**row)


def record_crawl_completion(
    result: dict[str, Any],
    started: float,
    *,
    event: str,
    logger: logging.Logger,
) -> None:
    results = result.get("results", [])
    get_metrics().observe_crawl(
        results=results,
        elapsed_ms=(time.monotonic() - started) * 1000,
    )
    logger.info(event, extra={"url_count": len(results)})


__all__ = ["_build_job_status", "record_crawl_completion"]
