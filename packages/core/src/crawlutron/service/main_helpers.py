"""Shared core helpers for API endpoints: job-status shaping and the request gate.

The service module carries the live ``readiness_state`` global the suites patch.
"""

import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from contextvars import ContextVar

from crawlutron.delta import DeltaContext
from crawlutron.models.job_status_response import JobStatusResponse
from crawlutron.models.result_record import ProducerEnvelope
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.runtime.metrics import get_metrics
from crawlutron.runtime.readiness_state import READINESS_STATE as readiness_state
from crawlutron.service.errors import CapacityExceeded, DomainError, ValidationRefusal
from crawlutron.service.response_shaping import aunified_response_from_result
from crawlutron.storage.job_store import JobStatusView
from crawlutron.storage.result_store import CrawlResultStore

_gate_token: ContextVar[str | None] = ContextVar("crawlutron_request_gate_token", default=None)


def build_job_status(view: JobStatusView) -> JobStatusResponse:
    """Build a ``JobStatusResponse`` from a store status view.

    Taking the view (a declared ``TypedDict``) keeps counts in the store's projection, not
    ``len()`` over history; the ``**`` splat fuses view and model -- pyright flags any undeclared key.
    """
    return JobStatusResponse(**view)


async def record_crawl_completion(
    result: ProducerEnvelope,
    started: float,
    *,
    event: str,
    logger: logging.Logger,
    caller_id: str = "",
) -> None:
    results = result.get("results", [])
    await get_metrics().observe_crawl(
        results=results,
        elapsed_ms=(time.monotonic() - started) * 1000,
        caller_id=caller_id,
    )
    logger.info(event, extra={"url_count": len(results)})


async def crawl_tail(
    result: ProducerEnvelope,
    started: float,
    *,
    event: str,
    logger: logging.Logger,
    caller_id: str = "",
    delta_context: DeltaContext | None = None,
    result_store: CrawlResultStore | None = None,
    owner: str = "",
) -> UnifiedCrawlResponse:
    """The shared crawl tail: completion metric, unified response, fail-open
    store put. Both doors call it; a third door cannot half-tail."""
    await record_crawl_completion(
        result,
        started,
        event=event,
        logger=logger,
        caller_id=caller_id,
    )
    response_model = await aunified_response_from_result(result, delta_context=delta_context)
    if result_store is not None:
        try:
            await result_store.aput(result["request_id"], response_model.model_dump(), owner=owner)
        except Exception:
            logger.warning("crawl_result_store_failed", extra={"request_id": result["request_id"]})
    return response_model


@asynccontextmanager
async def request_gate(
    event: str,
    *,
    logger: logging.Logger,
    reject: Callable[[ValueError], Awaitable[DomainError]] | None = None,
):
    """Admit one readiness-tracked request and own the whole tail: the
    ``begin_request`` admission, the rejection envelope, and the ``end_request``
    release. A fixed-cap refusal happens before admission; re-entry is a defect.
    """
    if _gate_token.get() is not None:
        raise RuntimeError(f"request_gate re-entered inside gate {event!r}: one request is one slot")
    _gate_token.set(event)
    try:
        if not readiness_state.begin_request():
            raise CapacityExceeded()
        try:
            yield
        except ValueError as error:
            logger.warning(event, extra={"error": str(error)})
            refused = await reject(error) if reject is not None else _validation_refusal(error)
            raise refused from error
        finally:
            readiness_state.end_request()
    finally:
        _gate_token.set(None)


__all__ = ["build_job_status", "crawl_tail", "record_crawl_completion", "request_gate"]


def _validation_refusal(error: ValueError) -> ValidationRefusal:
    """The default ValueError refusal: the shared 422 validation envelope."""
    return ValidationRefusal(
        "validation_error",
        "Request validation failed",
        {"errors": [{"msg": str(error)}]},
    )
