"""``/v1/crawl`` endpoint handlers (sync + async + result retrieval).

Extracted from the previous monolith in ``app/main.py`` so the
handler logic and the FastAPI URL→handler mapping live in different
modules. The handlers are exposed as plain async functions; the router
in :mod:`app.api.crawl_router` calls them and wires ``Depends`` for
``settings`` / ``job_store`` / ``result_store`` / ``shutdown_event``.

The ``READINESS_STATE`` singleton is imported directly so the
``begin_request`` / ``end_request`` gate does not require a closure.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable
from typing import Any

from fastapi import BackgroundTasks, HTTPException, Request
from fastapi.responses import JSONResponse, Response

from app.api.crawl_executor import execute_crawl
from app.api.exceptions import http_error, validation_http_error
from app.api.main_helpers import record_crawl_completion, request_gate
from app.api.response_shaping import _aunified_response_from_result
from app.api.webhooks import validate_webhook_url_async
from app.api_models import CrawlRequest, UnifiedCrawlResponse
from app.crawl.ids import new_request_id
from app.crawl.job_store import TERMINAL_STATUSES, JobKind, JobStore
from app.crawl.payload_helpers import checked_payload_urls
from app.crawl.policy import read_policy
from app.crawl.result_store import CrawlResultStore
from app.metrics import get_metrics
from app.output_formats import serialize_results
from app.settings import CrawlerSettings
from app.validation import HostPolicy

logger = logging.getLogger(__name__)


async def await_or_client_gone(work: Awaitable[Any], request: Request, *, poll_interval: float = 0.25) -> Any:
    """Await the sync crawl but cancel it if the client disconnects first.

    A synchronous ``/v1/crawl`` would otherwise run to completion to feed a
    result nobody will read (the client-abandon case). Starlette does not cancel
    a normal handler on disconnect, so we poll ``is_disconnected`` alongside the
    crawl and cancel the work, letting the engine's cancellation cleanup free
    the slot/browser. The fast path costs nothing: a finished crawl returns
    before the first poll sleep.
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


def _serialize_or_reject(items: list[dict[str, Any]], output_format: str) -> tuple[str, str]:
    """Serialize ``items`` for ``output_format``, or refuse the way the API says.

    An unusable output format is a caller error, so it becomes the 422
    ``validation_error`` the contract names -- and it is the only edge where a
    refused format is allowed to surface. ``/v1/crawl`` serializes twice on
    purpose: once to validate the format before the sync/async branch commits
    to a shape, and once to produce the real body. Written inline at both
    sites, the refusal was a second thing each site could spell differently,
    and the async branch's copy is the one a caller is least able to see.

    Callers that only want the format checked pass no items and discard the
    result; the validation is the point, not the bytes.
    """
    try:
        return serialize_results(items, output_format)
    except ValueError as error:
        raise http_error(422, "validation_error", str(error)) from error


async def crawl(
    payload: CrawlRequest,
    background_tasks: BackgroundTasks,
    settings: CrawlerSettings,
    job_store: JobStore,
    result_store: CrawlResultStore,
    shutdown_event: asyncio.Event,
    request: Request | None = None,
) -> Any:
    """Execute a policy-driven crawl (sync or async webhook mode)."""
    with request_gate("crawl_request_rejected", logger=logger, reject=_crawl_rejection):
        if payload.output_format:
            # Validate the format before the branch commits to sync or async
            # the async path otherwise accepts a bad format and only
            # fails inside a detached task the caller can no longer inspect.
            _serialize_or_reject([], payload.output_format)
        payload_dict = payload.model_dump(exclude_none=True)
        # L4: gate the batch size at the boundary, before the 202 is ever
        # returned. The async branch otherwise accepts an oversized batch and
        # only fails it inside the detached background task, where the caller
        # can no longer see why. Same chokepoint the engine uses, so sync and
        # async cannot drift.
        host_policy = HostPolicy.from_settings(settings)
        checked = checked_payload_urls(
            payload_dict,
            settings.max_batch_size,
            host_policy=host_policy,
        )
        webhook_url = payload.webhook_url
        async_mode = payload.async_mode
        if async_mode or webhook_url:
            if webhook_url and (
                not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://"))
            ):
                raise http_error(422, "validation_error", "Invalid webhook_url")
            if webhook_url:
                await validate_webhook_url_async(webhook_url, host_policy=host_policy)
            request_id = new_request_id()
            # The store's tier, not ``settings``: this policy is about to be
            # written into that store, and a row is read back with the tier the
            # store holds. Deriving a tier here from the same settings the
            # store was built from is right by accident and wrong the moment
            # they differ -- and then the policy the caller submitted and the
            # one the job later runs disagree about every limit it omitted.
            await job_store.acreate_single_shot(
                request_id, checked[0], read_policy(payload_dict.get("policy"), job_store.tier)
            )
            from app.api.async_crawl_runner import run_async_crawl

            background_tasks.add_task(
                run_async_crawl,
                request_id,
                payload_dict,
                webhook_url,
                settings,
                job_store,
                result_store,
                shutdown_event,
                urls=checked,
            )
            get_metrics().observe_crawl(accepted=True)
            return JSONResponse(
                status_code=202,
                content={
                    "status": "accepted",
                    "request_id": request_id,
                    "status_url": f"/v1/crawl/{request_id}",
                    "webhook_url": webhook_url,
                },
            )
        _crawl_started = time.monotonic()
        # Disconnect-aware sync crawl: if the caller has gone away, halt the
        # engine call instead of crawling to completion for a dead client.
        crawl_awaitable = execute_crawl(
            payload_dict, settings=settings, shutdown_event=shutdown_event, urls=checked
        )
        if request is None:
            result = await crawl_awaitable
        else:
            result = await await_or_client_gone(crawl_awaitable, request)
        record_crawl_completion(
            result,
            _crawl_started,
            event="crawl_request_completed",
            logger=logger,
        )
        response_model = await _aunified_response_from_result(result)
        try:
            await result_store.aput(result["request_id"], response_model.model_dump())
        except Exception:
            logger.warning("crawl_result_store_failed", extra={"request_id": result["request_id"]})
        output_format = payload.output_format
        if output_format and output_format.lower() != "json":
            body, media_type = _serialize_or_reject(
                [u.model_dump() for u in response_model.results], output_format
            )
            return Response(content=body, media_type=media_type)
        return response_model


async def get_crawl_result(
    request_id: str,
    job_store,
    result_store,
) -> UnifiedCrawlResponse:
    """Retrieve a completed crawl response by request_id.

    Answers 200 only for a job in a terminal state. A job that has written its
    results but not yet been marked ``COMPLETED`` is still 202, because
    ``run_async_crawl`` performs those two writes in that order: reporting the
    result a moment early tells a polling client the crawl is finished while it
    is still running, and the job store it then reads says ``running``.
    """
    stored = await result_store.aget(request_id)
    if stored is not None:
        return UnifiedCrawlResponse(**stored)
    job = await job_store.aload(request_id)
    if job is not None and job.kind == JobKind.SINGLE_SHOT:
        if job.results and job.status in TERMINAL_STATUSES:
            return UnifiedCrawlResponse(**job.results[0])
        raise http_error(
            202,
            "pending",
            f"Job {request_id} is still {job.status.value}",
            {"status": job.status.value},
        )
    raise http_error(404, "not_found", f"Unknown or expired request_id: {request_id}")


__all__ = ["crawl", "get_crawl_result"]
