"""Shared core helpers for API endpoints: job-status shaping and the request gate.

The service module carries the live ``readiness_state`` global the suites patch.
"""

import logging
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from contextvars import ContextVar

from fastapi import HTTPException

from crawlutron.api.exceptions import http_error, validation_http_error
from crawlutron.health.readiness import READINESS_STATE as readiness_state
from crawlutron.models.job_status_response import JobStatusResponse
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.runtime.metrics import get_metrics
from crawlutron.storage.job_store import JobStatusView

_gate_token: ContextVar[str | None] = ContextVar("crawlutron_request_gate_token", default=None)


def build_job_status(view: JobStatusView) -> JobStatusResponse:
    """Build a ``JobStatusResponse`` from a store status view.

    Taking the view (a declared ``TypedDict``) keeps counts in the store's projection, not
    ``len()`` over history; the ``**`` splat fuses view and model -- pyright flags any undeclared key.
    """
    return JobStatusResponse(**view)


def record_crawl_completion(
    result: ProducerEnvelope,
    started: float,
    *,
    event: str,
    logger: logging.Logger,
    caller_id: str = "",
) -> None:
    results = result.get("results", [])
    get_metrics().observe_crawl(
        results=results,
        elapsed_ms=(time.monotonic() - started) * 1000,
        caller_id=caller_id,
    )
    logger.info(event, extra={"url_count": len(results)})


@contextmanager
def request_gate(
    event: str,
    *,
    logger: logging.Logger,
    reject: Callable[[ValueError], HTTPException] = validation_http_error,
) -> Generator[None, None, None]:
    """Admit one readiness-tracked request and own the whole tail: the
    ``begin_request`` admission, the rejection envelope, and the ``end_request``
    release. A fixed-cap refusal happens before admission; re-entry is a defect.
    """
    if _gate_token.get() is not None:
        raise RuntimeError(f"request_gate re-entered inside gate {event!r}: one request is one slot")
    _gate_token.set(event)
    try:
        if not readiness_state.begin_request():
            raise http_error(
                503,
                "capacity_exceeded",
                "Request refused: active requests are at the configured cap (CRAWLUTRON_MAX_ACTIVE_REQUESTS)",
            )
        try:
            yield
        except ValueError as error:
            logger.warning(event, extra={"error": str(error)})
            raise reject(error) from error
        finally:
            readiness_state.end_request()
    finally:
        _gate_token.set(None)


__all__ = ["build_job_status", "record_crawl_completion", "request_gate"]
