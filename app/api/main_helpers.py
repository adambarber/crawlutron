"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction and a gated endpoint's lifecycle:
``request_gate`` (admit, reject, release) and ``record_crawl_completion`` (bookkeeping).
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from fastapi import HTTPException

from app.api.exceptions import validation_http_error
from app.health.readiness import READINESS_STATE as readiness_state
from app.models.job_status_response import JobStatusResponse
from app.models.result_record import ProducerEnvelope
from app.runtime.metrics import get_metrics
from app.storage.job_store import JobStatusView


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
) -> Iterator[None]:
    """Admit one readiness-tracked request and own the whole tail: the ``begin_request``
    admission, the rejection (warning + 4xx envelope), and the ``end_request`` release in the
    ``finally`` -- exactly once per exit, not repeated on reject (``end_request`` clamps at zero).
    """
    readiness_state.begin_request()
    try:
        yield
    except ValueError as error:
        logger.warning(event, extra={"error": str(error)})
        raise reject(error) from error
    finally:
        readiness_state.end_request()


__all__ = ["build_job_status", "record_crawl_completion", "request_gate"]
