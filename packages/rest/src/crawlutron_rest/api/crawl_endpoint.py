"""``/v1/crawl`` endpoint handlers (sync + async + result retrieval)."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import TYPE_CHECKING

from fastapi import Request
from fastapi.responses import JSONResponse, Response
from safe_url.host_policy import HostPolicy

from crawlutron.admission.payload_helpers import checked_payload_urls
from crawlutron.config.settings import CrawlerSettings
from crawlutron.delta import DeltaContext
from crawlutron.extraction.output_formats import serialize_results
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.models.crawl_pending import CrawlPendingError
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.trace_response import TraceResponse
from crawlutron.models.webhook_target import WebhookTarget
from crawlutron.runtime.metrics import get_metrics
from crawlutron.service.caller_auth import CALLER_QUOTA_ADMISSION, CallerQuotaAdmission, Tracking
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.crawl_endpoint_cores import (
    delta_context_for,
    stored_response_payload,
)
from crawlutron.service.crawl_executor import execute_crawl
from crawlutron.service.errors import DomainError, ValidationRefusal
from crawlutron.service.job_lifecycle import start_tracked_task
from crawlutron.service.main_helpers import record_crawl_completion, request_gate
from crawlutron.service.response_shaping import aunified_response_from_result
from crawlutron.service.webhooks import ensure_https_webhook_url, validate_webhook_url_async
from crawlutron.storage.ids import new_request_id
from crawlutron.storage.job_store import FrontierEntry, JobStore
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from crawlutron_rest.api.exceptions import http_error

if TYPE_CHECKING:
    # Annotation-only vocabulary: the endpoint's signatures name the shared record
    # types; none of these is constructed or called at runtime here.
    from safe_url.normalized_url import NormalizedUrl

    from crawlutron.models.json_value import JsonValue
    from crawlutron.models.result_record import ProducerEnvelope
    from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse

_logger = logging.getLogger(__name__)


async def await_or_client_gone[Work](
    work: Awaitable[Work], request: Request, *, poll_interval: float = 0.25
) -> Work:
    """Await the sync crawl but cancel it if the client disconnects first:
    Starlette does not cancel a handler on disconnect, so poll ``is_disconnected``
    and cancel (freeing the slot/browser); a finished crawl returns untouched.
    """
    task = asyncio.ensure_future(work)
    try:
        while True:
            if task.done():
                return await task
            if await request.is_disconnected():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise http_error(499, "client_disconnected", "Client disconnected before the crawl completed")
            await asyncio.sleep(poll_interval)
    finally:
        # Handler cancelled (uvicorn shutdown) or any exit: the crawl task must not
        # outlive this frame holding its slot for a result nobody will read.
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _crawl_rejection(error: ValueError) -> DomainError:
    """``/v1/crawl``'s refusal: the shared 422 envelope, counted as a crawl error.

    This is the gate's ``reject`` hook, so the metric is part of refusing rather
    than a line a handler can forget once the reject tail stopped being its own.
    """
    get_metrics().observe_crawl(errors=1)
    return ValidationRefusal(
        "validation_error",
        "Request validation failed",
        {"errors": [{"msg": str(error)}]},
    )


# The delta loader and the stored-payload reader moved to service.crawl_endpoint_cores
# (#849); the HTTP doors below keep their wire shapes over the shared cores.


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
    spill_store: CrawlSpillStore | None,
    shutdown_event: asyncio.Event,
    request: Request | None = None,
    caller: CallerIdentity | None = None,
    concurrency_tracker: CallerConcurrencyTracker | None = None,
    admission: CallerQuotaAdmission | None = None,
) -> JSONResponse | Response | UnifiedCrawlResponse:
    """Execute a policy-driven crawl (sync or async webhook mode)."""
    port = CALLER_QUOTA_ADMISSION if admission is None else admission
    active_caller = caller if caller is not None else port.anonymous_caller()
    # One admission for both doors: sync holds a slot for the crawl's life; async
    # persists a durable job row. The whole dispatch stays under the gate.
    with request_gate("crawl_request_rejected", logger=_logger, reject=_crawl_rejection):
        if payload.output_format:
            # Format is validated pre-branch: the async path fails unseen.
            _serialize_or_reject([], payload.output_format)
        # Batch size is gated pre-202: an oversized async batch fails unseen.
        host_policy = HostPolicy.from_settings(settings)
        checked_pairs = checked_payload_urls(payload, settings.max_batch_size, host_policy=host_policy)
        delta_context = await delta_context_for(payload, active_caller.caller_id, result_store)
        return await _admitted_crawl(
            payload,
            active_caller=active_caller,
            admission=port,
            settings=settings,
            job_store=job_store,
            result_store=result_store,
            spill_store=spill_store,
            concurrency_tracker=concurrency_tracker,
            shutdown_event=shutdown_event,
            host_policy=host_policy,
            checked_pairs=checked_pairs,
            delta_context=delta_context,
            request=request,
            webhook_url=None
            if payload.webhook_url is None
            else WebhookTarget.admit(payload.webhook_url, host_policy=host_policy),
        )


async def _admitted_crawl(
    payload: CrawlRequest,
    *,
    active_caller: CallerIdentity,
    admission: CallerQuotaAdmission,
    settings: CrawlerSettings,
    job_store,
    result_store: CrawlResultStore,
    spill_store: CrawlSpillStore | None,
    concurrency_tracker: CallerConcurrencyTracker | None,
    shutdown_event: asyncio.Event,
    host_policy: HostPolicy,
    checked_pairs: list[tuple[str, NormalizedUrl]],
    delta_context: DeltaContext | None,
    request: Request | None,
    webhook_url: WebhookTarget | None,
) -> JSONResponse | Response | UnifiedCrawlResponse:
    """One admission for both doors: sync holds a slot; async persists a durable job row."""
    async_door = bool(payload.async_mode or webhook_url)
    tracking = Tracking(job_store=job_store, concurrency_tracker=concurrency_tracker)
    async with admission.admit(
        active_caller,
        payload.policy.limits.max_pages,
        tracking=tracking,
        payload=payload,
        reserve_slot=not async_door,
    ) as owner:
        if async_door:
            crawl_awaitable = None
        else:
            crawl_awaitable = execute_crawl(
                payload,
                settings=settings,
                shutdown_event=shutdown_event,
                url_pairs=checked_pairs,
                spill_store=spill_store,
            )
        if async_door:
            return await _accepted_async(
                payload,
                settings=settings,
                job_store=job_store,
                result_store=result_store,
                spill_store=spill_store,
                shutdown_event=shutdown_event,
                host_policy=host_policy,
                checked=checked_pairs,
                owner=owner,
                webhook_url=webhook_url,
                delta_context=delta_context,
            )
        return await _run_sync_crawl(
            payload,
            result_store=result_store,
            checked=checked_pairs,
            owner=owner,
            request=request,
            crawl_awaitable=crawl_awaitable,
            delta_context=delta_context,
        )


async def _accepted_async(
    payload: CrawlRequest,
    *,
    settings: CrawlerSettings,
    job_store: JobStore,
    result_store: CrawlResultStore,
    spill_store: CrawlSpillStore | None,
    shutdown_event: asyncio.Event,
    host_policy: HostPolicy,
    checked: list[tuple[str, NormalizedUrl]],
    owner: str,
    webhook_url: WebhookTarget | None,
    delta_context: DeltaContext | None,
) -> JSONResponse:
    """The 202 door: validate the webhook, persist the job row, start the runner."""
    if webhook_url is not None:
        delivery = webhook_url.delivery_url()
        try:
            ensure_https_webhook_url(delivery)
        except ValueError as error:
            raise http_error(422, "validation_error", str(error)) from error
        await validate_webhook_url_async(delivery, host_policy=host_policy)
    request_id = new_request_id()
    # The store's tier, not ``settings``: the row is read back with the
    # store's tier, and two tier sources drift apart the moment they differ.
    batch = [FrontierEntry(url=url, depth=0) for _, url in checked]
    await job_store.acreate_single_shot(
        request_id, checked[0][0], payload.policy, owner=owner, frontier=batch
    )
    from crawlutron.service.async_crawl_runner import run_async_crawl

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
            spill_store=spill_store,
            delta_context=delta_context,
        ),
    )
    get_metrics().observe_crawl(accepted=True, caller_id=owner)
    return JSONResponse(
        status_code=202,
        content={
            "status": "accepted",
            "request_id": request_id,
            "status_url": f"/v1/crawl/{request_id}",
            "webhook_url": None if webhook_url is None else webhook_url.redacted(),
        },
    )


async def _run_sync_crawl(
    payload: CrawlRequest,
    *,
    result_store: CrawlResultStore,
    checked: list[tuple[str, NormalizedUrl]],
    owner: str,
    request: Request | None,
    crawl_awaitable: Awaitable[ProducerEnvelope] | None,
    delta_context: DeltaContext | None,
) -> Response | UnifiedCrawlResponse:
    """The sync door: disconnect-aware crawl, metrics, store, then the body."""
    if crawl_awaitable is None:
        # Unreachable from the dispatch: the sync door always receives a built awaitable.
        raise AssertionError("the sync door always receives a built awaitable")
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
    response_model = await aunified_response_from_result(result, delta_context=delta_context)
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


# The stored-payload reader lives in service.crawl_endpoint_cores (#849); the
# replay doors map its typed refusals to the wire below.


async def _stored_payload_or_not_found(
    request_id: str,
    job_store,
    result_store,
    caller_id: str = "",
) -> JsonValue:
    """The stored payload for ``request_id``, or the mapped refusal.

    Both replay doors answer the same miss the same way, and the second copy of
    the check is a second place the message shape can drift.
    """
    try:
        payload = await stored_response_payload(request_id, job_store, result_store, caller_id)
    except CrawlPendingError as error:
        raise http_error(
            202,
            "pending",
            f"Job {error.request_id} is still {error.job_status}",
            {"status": str(error.job_status)},
        ) from error
    if payload is None:
        # The REST wire envelope the replay doors have always answered; the MCP
        # read core raises the same typed refusal for its own door.
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


__all__ = [
    "await_or_client_gone",
    "crawl",
    "get_crawl_result",
    "get_crawl_trace",
]
