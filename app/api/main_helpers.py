"""Shared helpers for main.py API endpoints.

Centralizes JobStatusResponse construction, and the two halves of a gated
endpoint's lifecycle: :func:`request_gate` (admit, reject, release) and
:func:`record_crawl_completion` (the accepted/completed bookkeeping).
"""

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

from fastapi import HTTPException

from app.api.exceptions import validation_http_error
from app.api_responses import (
    JobStatusResponse,
)
from app.health.readiness import READINESS_STATE as readiness_state
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


@contextmanager
def request_gate(
    event: str,
    *,
    logger: logging.Logger,
    reject: Callable[[ValueError], HTTPException] = validation_http_error,
) -> Iterator[None]:
    """Admit one readiness-tracked request and own everything that must happen to it.

    Three obligations belong to the same tail, so they live here together and a
    handler spells none of them: the ``begin_request`` admission, the rejection
    (operator warning + the 4xx envelope), and the ``end_request`` release.
    Written per endpoint, each was droppable independently -- a reject that
    forgot the release leaks a readiness slot for the life of the process, and
    one that forgot the warning loses the only operator signal that a caller was
    turned away.

    The release lives in the ``finally`` and nowhere else, so it happens
    exactly once on every exit: admit, reject, success, and an unhandled 500
    alike. It is deliberately NOT also called on the reject path, because
    ``end_request`` clamps at zero -- a second decrement would silently cancel
    a slot another request is legitimately holding.

    ``reject`` is the endpoint's own refusal policy, and it is the only part
    that varies: /v1/render answers 400 with the ValueError text as the message
    where the others answer 422 with the shared error detail list. Naming it
    per endpoint is why the default can be the shared envelope rather than an
    average of the three.
    """
    readiness_state.begin_request()
    try:
        yield
    except ValueError as error:
        logger.warning(event, extra={"error": str(error)})
        raise reject(error) from error
    finally:
        readiness_state.end_request()


__all__ = ["_build_job_status", "record_crawl_completion", "request_gate"]
