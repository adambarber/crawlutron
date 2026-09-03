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

import logging
import time
import uuid
from typing import Any

from fastapi import BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, Response

from app.api.crawl_executor import execute_crawl
from app.api.response_shaping import build_unified_result
from app.api.webhooks import validate_webhook_url
from app.api_models import CrawlRequest, UnifiedCrawlResponse
from app.crawl.job_store import JobKind
from app.health.readiness import READINESS_STATE as readiness_state
from app.metrics import get_metrics
from app.output_formats import serialize_results

logger = logging.getLogger(__name__)


async def crawl(
    payload: CrawlRequest,
    background_tasks: BackgroundTasks,
    settings,
    job_store,
    result_store,
    shutdown_event,
) -> Any:
    """Execute a policy-driven crawl (sync or async webhook mode)."""
    readiness_state.begin_request()
    try:
        if payload.output_format:
            try:
                serialize_results([], payload.output_format)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "validation_error", "message": str(error), "details": {}},
                ) from error
        payload_dict = payload.model_dump(exclude_none=True)
        webhook_url = payload.webhook_url
        async_mode = payload.async_mode
        if async_mode or webhook_url:
            if webhook_url and (
                not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://"))
            ):
                raise HTTPException(
                    status_code=422,
                    detail={"code": "validation_error", "message": "Invalid webhook_url", "details": {}},
                )
            if webhook_url:
                validate_webhook_url(webhook_url)
            request_id = f"crawl_{uuid.uuid4().hex[:12]}"
            job_store.create_single_shot(request_id, payload_dict.get("url", ""), payload_dict)
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
        result = await execute_crawl(payload_dict, settings=settings, shutdown_event=shutdown_event)
        get_metrics().observe_crawl(
            results=result.get("results", []),
            elapsed_ms=(time.monotonic() - _crawl_started) * 1000,
        )
        logger.info("crawl_request_completed", extra={"url_count": len(result.get("results", []))})
        unified = [build_unified_result(item) for item in result.get("results", [])]
        response_model = UnifiedCrawlResponse(
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=result["policy_schema_version"],
            success=True,
            results=unified,
        )
        try:
            result_store.put(result["request_id"], response_model.model_dump())
        except Exception:  # noqa: BLE001 - retrieval is best-effort
            logger.warning("crawl_result_store_failed", extra={"request_id": result["request_id"]})
        output_format = payload.output_format
        if output_format and output_format.lower() != "json":
            try:
                body, media_type = serialize_results([u.model_dump() for u in unified], output_format)
            except ValueError as error:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "validation_error", "message": str(error), "details": {}},
                ) from error
            return Response(content=body, media_type=media_type)
        return UnifiedCrawlResponse(
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=result["policy_schema_version"],
            success=True,
            results=unified,
        )
    except ValueError as error:
        get_metrics().observe_crawl(errors=1)
        logger.warning("crawl_request_rejected", extra={"error": str(error)})
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": [{"msg": str(error)}]},
            },
        ) from error
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
        raise HTTPException(
            status_code=202,
            detail={
                "code": "pending",
                "message": f"Job {request_id} is still {job.status.value}",
                "details": {"status": job.status.value},
            },
        )
    raise HTTPException(
        status_code=404,
        detail={
            "code": "not_found",
            "message": f"Unknown or expired request_id: {request_id}",
            "details": {},
        },
    )


__all__ = ["crawl", "get_crawl_result"]
