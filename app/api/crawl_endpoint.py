"""``/v1/crawl`` endpoint handlers (sync + async + result retrieval)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.admission.payload_helpers import checked_payload_urls
from app.api.caller_auth import Tracking, admit, anonymous_caller
from app.api.caller_concurrency import CallerConcurrencyTracker
from app.api.crawl_executor import execute_crawl
from app.api.exceptions import http_error, validation_http_error
from app.api.job_runner import start_tracked_task
from app.api.main_helpers import record_crawl_completion, request_gate
from app.api.response_shaping import aunified_response_from_result
from app.api.webhooks import ensure_https_webhook_url, validate_webhook_url_async
from app.config.settings import CrawlerSettings
from app.extraction.output_formats import serialize_results
from app.models.caller_identity import CallerIdentity
from app.models.crawl_request import CrawlRequest
from app.models.host_policy import HostPolicy
from app.models.json_value import JsonValue
from app.models.normalized_url import NormalizedUrl
from app.models.result_record import ProducerEnvelope
from app.models.trace_response import TraceResponse
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.runtime.metrics import get_metrics
from app.storage.ids import new_request_id
from app.storage.job_store import TERMINAL_STATUSES, JobKind, JobStore
from app.storage.result_store import CrawlResultStore

_logger = logging.getLogger(__name__)


async def await_or_client_gone[Work](
    work: Awaitable[Work], request: Request, *, poll_interval: float = 0.25
) -> Work:
    """Await the sync crawl but cancel it if the client disconnects first:
    Starlette does not cancel a handler on disconnect, so poll ``is_disconnected``
    and cancel (freeing the slot/browser); a finished crawl returns untouched.
    """
    task = asyncio.ensure_future(work)
    while True:
        if task.done():
            return await task
        if await request.is_disconnected():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            raise http_error(499, "client_disconnected", "Client disconnected before the crawl completed")
        await asyncio.sleep(poll_interval)


def _crawl_rejection(error: ValueError) -> HTTPException:
    """``/v1/crawl``'s refusal: the shared 422 envelope, counted as a crawl error.

    This is the gate's ``reject`` hook, so the metric is part of refusing rather
    than a line a handler can forget once the reject tail stopped being its own.
    """
    get_metrics().observe_crawl(errors=1)
    return validation_http_error(error)


def _serialize_or_reject(items: list[dict[str, JsonValue]], output_format: str) -> tuple[str, str]:
    """Serialize ``items`` for ``output_format``, or refuse as the 422 the
    contract names -- the one edge a refused format surfaces. Callers that only
    want the format checked pass no items; the validation is the point, not bytes.
    """
    try:
        return serialize_results(items, output_format)
    except ValueError as error:
        raise http_error(422, "validation_error", str(error)) from error


async def crawl(
    payload: CrawlRequest,
    settings: CrawlerSettings,
    job_store: JobStore,
    result_store: CrawlResultStore,
    shutdown_event: asyncio.Event,
    request: Request | None = None,
    caller: CallerIdentity | None = None,
    concurrency_tracker: CallerConcurrencyTracker | None = None,
) -> JSONResponse | Response | UnifiedCrawlResponse:
    """Execute a policy-driven crawl (sync or async webhook mode)."""
    active_caller = caller if caller is not None else anonymous_caller()
    # One admission for both doors: sync holds a slot for the crawl's life; async
    # persists a durable job row. The whole dispatch stays under the gate.
    with request_gate("crawl_request_rejected", logger=_logger, reject=_crawl_rejection):
        if payload.output_format:
            # Format is validated pre-branch: the async path fails unseen.
            _serialize_or_reject([], payload.output_format)
        # Batch size is gated pre-202: an oversized async batch fails unseen.
        host_policy = HostPolicy.from_settings(settings)
        checked_pairs = checked_payload_urls(payload, settings.max_batch_size, host_policy=host_policy)
        return await _admitted_crawl(
            payload,
            active_caller=active_caller,
            settings=settings,
            job_store=job_store,
            result_store=result_store,
            concurrency_tracker=concurrency_tracker,
            shutdown_event=shutdown_event,
            host_policy=host_policy,
            checked_pairs=checked_pairs,
            request=request,
            webhook_url=payload.webhook_url,
        )


async def _admitted_crawl(
    payload: CrawlRequest,
    *,
    active_caller: CallerIdentity,
    settings: CrawlerSettings,
    job_store,
    result_store: CrawlResultStore,
    concurrency_tracker: CallerConcurrencyTracker | None,
    shutdown_event: asyncio.Event,
    host_policy: HostPolicy,
    checked_pairs: list[tuple[str, NormalizedUrl]],
    request: Request | None,
    webhook_url: str | None,
) -> JSONResponse | Response | UnifiedCrawlResponse:
    """One admission for both doors: sync holds a slot for the crawl's life; async
    persists a durable job row. Gate, admit, and dispatch stay one block."""
    with request_gate("crawl_request_rejected", logger=_logger, reject=_crawl_rejection):
        async with admit(
            active_caller,
            payload.policy.limits.max_pages,
            tracking=Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker),
            payload=payload,
            reserve_slot=not (payload.async_mode or webhook_url),
        ) as owner:
            # Built after admission (a slot exists); calling runs nothing.
            crawl_awaitable = (
                None
                if payload.async_mode or webhook_url
                else execute_crawl(
                    payload, settings=settings, shutdown_event=shutdown_event, url_pairs=checked_pairs
                )
            )
            if payload.async_mode or webhook_url:
                return await _accepted_async(
                    payload,
                    settings=settings,
                    job_store=job_store,
                    result_store=result_store,
                    shutdown_event=shutdown_event,
                    host_policy=host_policy,
                    checked=checked_pairs,
                    owner=owner,
                    webhook_url=webhook_url,
                )
            return await _run_sync_crawl(
                payload,
                result_store=result_store,
                checked=checked_pairs,
                owner=owner,
                request=request,
                crawl_awaitable=crawl_awaitable if crawl_awaitable is not None else _missing_sync_awaitable(),
            )


def _missing_sync_awaitable() -> Awaitable[ProducerEnvelope]:
    """Unreachable: the dispatch passes None only when the async door is taken."""
    raise AssertionError("the sync door always receives a built awaitable")


async def _accepted_async(
    payload: CrawlRequest,
    *,
    settings: CrawlerSettings,
    job_store: JobStore,
    result_store: CrawlResultStore,
    shutdown_event: asyncio.Event,
    host_policy: HostPolicy,
    checked: list[tuple[str, NormalizedUrl]],
    owner: str,
    webhook_url: str | None,
) -> JSONResponse:
    """The 202 door: validate the webhook, persist the job row, start the runner."""
    if webhook_url and (
        not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://"))
    ):
        raise http_error(422, "validation_error", "Invalid webhook_url")
    if webhook_url:
        try:
            ensure_https_webhook_url(webhook_url)
        except ValueError as error:
            raise http_error(422, "validation_error", str(error)) from error
        await validate_webhook_url_async(webhook_url, host_policy=host_policy)
    request_id = new_request_id()
    # The store's tier, not ``settings``: the row is read back with the
    # store's tier, and two tier sources drift apart the moment they differ.
    await job_store.acreate_single_shot(
        request_id,
        checked[0][0],
        payload.policy,
        owner=owner,
    )
    from app.api.async_crawl_runner import run_async_crawl

    start_tracked_task(
        request_id,
        lambda: run_async_crawl(
            request_id,
            payload,
            webhook_url,
            settings,
            job_store,
            result_store,
            shutdown_event,
            url_pairs=checked,
        ),
    )
    get_metrics().observe_crawl(accepted=True, caller_id=owner)
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "request_id": request_id,
            "status_url": f"/v1/crawl/{request_id}",
            "webhook_url": webhook_url,
        },
    )


async def _run_sync_crawl(
    payload: CrawlRequest,
    *,
    result_store: CrawlResultStore,
    checked: list[tuple[str, NormalizedUrl]],
    owner: str,
    request: Request | None,
    crawl_awaitable: Awaitable[ProducerEnvelope],
) -> Response | UnifiedCrawlResponse:
    """The sync door: disconnect-aware crawl, metrics, store, then the body."""
    _crawl_started = time.monotonic()
    # Disconnect-aware sync crawl: if the caller has gone away, halt the
    # engine call instead of crawling to completion for a dead client.
    if request is None:
        result = await crawl_awaitable
    else:
        result = await await_or_client_gone(crawl_awaitable, request)
    record_crawl_completion(
        result,
        _crawl_started,
        event="crawl_request_completed",
        logger=_logger,
        caller_id=owner,
    )
    response_model = await aunified_response_from_result(result)
    try:
        await result_store.aput(result["request_id"], response_model.model_dump(), owner=owner)
    except Exception:
        _logger.warning("crawl_result_store_failed", extra={"request_id": result["request_id"]})
    output_format = payload.output_format
    if output_format and output_format.lower() != "json":
        body, media_type = _serialize_or_reject(
            [u.model_dump() for u in response_model.results], output_format
        )
        return Response(content=body, media_type=media_type)
    return response_model


async def _stored_response_payload(
    request_id: str,
    job_store,
    result_store,
    caller_id: str = "",
) -> JsonValue | None:
    """The stored response for ``request_id``, or None, shared by both replay
    doors: a job not in a terminal state raises 202; the sync path with no job
    answers from the store. Reads scope to ``caller_id``; foreign rows miss.
    """
    job = await job_store.aload_for_caller(request_id, caller_id)
    if job is not None and job.status not in TERMINAL_STATUSES:
        raise http_error(
            202,
            "pending",
            f"Job {request_id} is still {job.status.value}",
            {"status": job.status.value},
        )
    stored = await result_store.aget_for_caller(request_id, caller_id)
    if stored is not None:
        return stored
    if job is not None and job.kind == JobKind.SINGLE_SHOT and job.results:
        # Same wire payload, written by the runner to the job's results. It is
        # also the durable copy: the result cache expires, the job row does not.
        return job.results[0]
    return None


async def _stored_payload_or_not_found(
    request_id: str,
    job_store,
    result_store,
    caller_id: str = "",
) -> JsonValue:
    """The stored payload for ``request_id``, or a 404.

    Both replay doors answer the same miss the same way, and the second copy of
    the check is a second place the message shape can drift.
    """
    payload = await _stored_response_payload(request_id, job_store, result_store, caller_id)
    if payload is None:
        raise http_error(404, "not_found", f"Unknown or expired request_id: {request_id}")
    return payload


async def get_crawl_result(
    request_id: str,
    job_store,
    result_store,
    caller: CallerIdentity | None = None,
) -> Response:
    """Retrieve a completed crawl response by request_id; a replay is bytes.

    Serving the stored dump answers byte-for-byte; rebuilding would re-validate
    URL fields, and admission is the constructor's ingress job, not the cache's.
    """
    caller_id = caller.caller_id if caller is not None else ""
    return JSONResponse(
        content=await _stored_payload_or_not_found(request_id, job_store, result_store, caller_id)
    )


async def get_crawl_trace(
    request_id: str,
    job_store,
    result_store,
    caller: CallerIdentity | None = None,
) -> TraceResponse:
    """The same stored record read as a trace: attempts and evidence, no bodies.

    A projection, not a second store -- it validates off the response envelope's
    dump, so it cannot claim an attempt the response does not have.
    """
    caller_id = caller.caller_id if caller is not None else ""
    payload = await _stored_payload_or_not_found(request_id, job_store, result_store, caller_id)
    return TraceResponse.model_validate(payload)


__all__ = ["await_or_client_gone", "crawl", "get_crawl_result", "get_crawl_trace"]
