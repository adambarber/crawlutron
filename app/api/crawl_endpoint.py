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

from fastapi import BackgroundTasks
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

from app.api.crawl_executor import execute_crawl
from app.api.exceptions import http_error, validation_http_error
from app.api.main_helpers import record_crawl_completion
from app.api.response_shaping import abuild_unified_crawl_response
from app.api.webhooks import validate_webhook_url_async
from app.api_models import CrawlRequest, UnifiedCrawlResponse
from app.crawl.ids import new_request_id
from app.crawl.job_store import JobKind
from app.crawl.payload_helpers import checked_payload_urls
from app.health.readiness import READINESS_STATE as readiness_state
from app.metrics import get_metrics
from app.output_formats import serialize_results
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


async def crawl(
    payload: CrawlRequest,
    background_tasks: BackgroundTasks,
    settings,
    job_store,
    result_store,
    shutdown_event,
    request: Request | None = None,
) -> Any:
    """Execute a policy-driven crawl (sync or async webhook mode)."""
    readiness_state.begin_request()
    try:
        if payload.output_format:
            try:
                serialize_results([], payload.output_format)
            except ValueError as error:
                raise http_error(422, "validation_error", str(error)) from error
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
            job_store.create_single_shot(request_id, checked[0], payload_dict)
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
        response_model = await abuild_unified_crawl_response(
            result.get("results", []),
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=result["policy_schema_version"],
        )
        try:
            result_store.put(result["request_id"], response_model.model_dump())
        except Exception:
            logger.warning("crawl_result_store_failed", extra={"request_id": result["request_id"]})
        output_format = payload.output_format
        if output_format and output_format.lower() != "json":
            try:
                body, media_type = serialize_results(
                    [u.model_dump() for u in response_model.results], output_format
                )
            except ValueError as error:
                raise http_error(422, "validation_error", str(error)) from error
            return Response(content=body, media_type=media_type)
        return response_model
    except ValueError as error:
        get_metrics().observe_crawl(errors=1)
        logger.warning("crawl_request_rejected", extra={"error": str(error)})
        raise validation_http_error(error) from error
    finally:
        readiness_state.end_request()


async def get_crawl_result(
    request_id: str,
    job_store,
    result_store,
) -> UnifiedCrawlResponse:
    """Retrieve a completed crawl response by request_id."""
    stored = result_store.get(request_id)
    if stored is not None:
        return UnifiedCrawlResponse(**stored)
    job = job_store.load(request_id)
    if job is not None and job.kind == JobKind.SINGLE_SHOT:
        if job.results:
            return UnifiedCrawlResponse(**job.results[0])
        raise http_error(
            202,
            "pending",
            f"Job {request_id} is still {job.status.value}",
            {"status": job.status.value},
        )
    raise http_error(404, "not_found", f"Unknown or expired request_id: {request_id}")


__all__ = ["crawl", "get_crawl_result"]
