from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from app.crawl.engine import crawl_request

# Standalone JSON-line logging configuration. The previous version of this
# module imported ``local_service_logging`` from the parent monorepo's
# ``shared/python`` tree. The standalone equivalent now lives at
# ``app/logging_config.py``; integration deployments that want richer
# logging can override ``configure_service_logging`` in their own
# integration layer.
from app.logging_config import configure_service_logging

from .api.crawl_executor import execute_crawl as _execute_crawl
from .api.policy_defaults import BROWSER_FALLBACK_POLICY, DOCUMENT_EXTRACTION_POLICY
from .api.response_shaping import (
    build_unified_result as _build_unified_result,
)
from .api.webhooks import (
    validate_webhook_url as _validate_webhook_url,
)
from .api_models import (
    API_VERSION,
    POLICY_SCHEMA_VERSION,
    CrawlerError,
    CrawlRequest,
    UnifiedCrawlResponse,
)
from .crawl.job_store import JobKind, JobStore
from .crawl.result_store import CrawlResultStore
from .crawl.strategies.browser_pool import (
    close_browser_pool,
    get_browser_pool,
    initialize_browser_pool,
)
from .metrics import get_metrics
from .output_formats import serialize_results
from .settings import CrawlerSettings

try:
    from fastapi import BackgroundTasks, Body, FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse, Response
except ImportError:
    Body = None  # type: ignore[assignment]
    FastAPI = None  # type: ignore[assignment]
    HTTPException = Exception  # type: ignore[assignment]
    JSONResponse = None  # type: ignore[assignment]
    Request = object  # type: ignore[assignment]
    RequestValidationError = Exception  # type: ignore[assignment]


# Import modular routers
from .discovery.agent_hints import create_agent_hints_router
from .discovery.capabilities import create_capabilities_router
from .discovery.well_known import create_well_known_router
from .health.readiness import create_health_router

settings = CrawlerSettings.from_mapping()

# Job store for resumable crawls (#3+#14)
job_store = JobStore(settings.cache_dir + "/jobs.db")

# Completed single-request crawl results (fixes the dead status_url promise and
# makes results recoverable if the HTTP connection drops mid-response).
result_store = CrawlResultStore(
    settings.cache_dir + "/results.db",
    ttl_seconds=float(os.environ.get("BROWSER_CRAWLER_RESULT_TTL_SECONDS", "3600")),
    max_entries=int(os.environ.get("BROWSER_CRAWLER_RESULT_MAX_ENTRIES", "1000")),
)

# Ensure the parent directory of the log file exists before the logger tries
# to open it; standalone checkouts default to ./logs which may not exist.
if settings.log_file_path:
    os.makedirs(os.path.dirname(settings.log_file_path) or ".", exist_ok=True)
logger = configure_service_logging(
    service_name="browser-crawler",
    log_level=settings.log_level,
    log_file_path=settings.log_file_path,
    log_file_max_bytes=settings.log_file_max_bytes,
    log_file_backup_count=settings.log_file_backup_count,
)
# The single browser pool lives in the browser_pool module (set up in lifespan
# via initialize_browser_pool); request handlers reach it through
# get_browser_pool(). There is intentionally no second pool global here.
shutdown_event = asyncio.Event()
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.setLevel(logger.level)

# Third-party extraction libraries log per-element WARNINGs that are expected
# parser noise (e.g. trafilatura on <a> elements without href) and cannot be
# acted on. Keep ERROR+ visible, drop the chatter.
logging.getLogger("trafilatura").setLevel(logging.ERROR)


def build_unified_crawl_response(results: list[dict[str, Any]]) -> UnifiedCrawlResponse:
    """Backward-compatible alias: build the unified response (was CondensedCrawlResponse)."""
    return UnifiedCrawlResponse(
        request_id="crawl_unified",
        submitted_count=len(results),
        policy_schema_version=POLICY_SCHEMA_VERSION,
        success=True,
        results=[_build_unified_result(item) for item in results],
    )


if FastAPI is not None:

    @asynccontextmanager
    async def _lifespan(_app: FastAPI):
        # Startup: wire webhook helpers, initialize browser pool, load cookie jar.
        from .api.async_crawl_runner import _get_async_crawl_semaphore
        from .api.webhooks import set_webhook_dependencies

        set_webhook_dependencies(
            webhook_semaphore=_get_async_crawl_semaphore(),
            shutdown_event=shutdown_event,
            crawl_request_fn=crawl_request,
            pool_getter=get_browser_pool,
            unified_response_cls=UnifiedCrawlResponse,
            build_unified_result=_build_unified_result,
            get_metrics=get_metrics,
        )
        await initialize_browser_pool(settings)
        if settings.cookie_persistence_enabled:
            # Create the parent directory before the cookie jar tries to read
            # from it; standalone checkouts default to ./cache which may not
            # exist on first run.
            os.makedirs(os.path.dirname(settings.cookie_jar_path) or ".", exist_ok=True)
            from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR

            _GLOBAL_COOKIE_JAR.load_from_file(settings.cookie_jar_path)

        yield

        # Shutdown: save cookie jar if persistence enabled, then close clients and browser pool
        from .api.webhooks import clear_webhook_dependencies

        clear_webhook_dependencies()
        if settings.cookie_persistence_enabled:
            from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR

            _GLOBAL_COOKIE_JAR.save_to_file(settings.cookie_jar_path)

        from app.crawl.strategies.http import close_async_http_client

        shutdown_event.set()
        await close_async_http_client()
        await close_browser_pool()

    app = FastAPI(
        title="Browser Crawler Execution Service",
        lifespan=_lifespan,
        description=(
            "Localhost-only policy-driven crawler execution service for municipal script tools. "
            "GET /health is lightweight liveness for Docker healthchecks; GET /ready reports active crawl "
            "saturation without implying process death. Blocking direct HTTP, document extraction, Trafilatura, "
            "and synchronous browser work run off the FastAPI event loop so liveness stays responsive during crawls. "
            "Clients submit resolved crawl policies, inspect per-URL provenance, and use attempt history "
            "to make reviewable policy suggestions rather than mutating host policy automatically. "
            "The service classifies Cloudflare/WAF/CAPTCHA/login/paywall outcomes but does not bypass, solve, "
            "outsource, rotate identities, rotate proxies, or inject credentials."
        ),
        version=API_VERSION,
        contact={"name": "browser-crawler"},
        license_info={"name": "MIT License"},
        openapi_tags=[
            {
                "name": "Health",
                "description": "Lightweight liveness and crawl-capacity readiness endpoints for local orchestration.",
            },
            {
                "name": "Discovery",
                "description": "Machine-readable service capabilities and AI agent guidance.",
            },
            {
                "name": "Crawler",
                "description": "Browser render and universal policy-driven crawl execution endpoints.",
            },
        ],
    )
    # Stash shared dependencies on ``app.state`` so router handlers outside
    # this module can fetch them via :func:`get_settings` /
    # :func:`get_shutdown_event` FastAPI dependencies.
    app.state.crawler_settings = settings
    app.state.shutdown_event = shutdown_event
    app.state.job_store = job_store
    app.state.result_store = result_store

    # Body-size guard: cap request bodies to a sane upper bound so a malicious
    # caller cannot exhaust memory with a 1GB JSON before pydantic validation
    # even runs (adversarial review F-no-body-size-limit). 16 MiB is well
    # above any legitimate crawl request (URLs + policy + recursive metadata)
    # while still bounding the per-request RAM exposure. The middleware
    # caps the inbound byte stream at the ASGI receive layer so FastAPI's
    # downstream body handling still works without us buffering the body.
    _MAX_REQUEST_BODY_BYTES = 16 * 1024 * 1024

    @app.middleware("http")
    async def _limit_request_body(request: Request, call_next):
        # Read Content-Length up front when present (no body read yet).
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > _MAX_REQUEST_BODY_BYTES:
                    return JSONResponse(
                        status_code=413,
                        content={
                            "code": "request_too_large",
                            "message": "Request body exceeds the maximum allowed size",
                            "details": {"max_bytes": _MAX_REQUEST_BODY_BYTES},
                        },
                    )
            except ValueError:
                pass
        # Cap any chunked / lying-CL request by wrapping receive() so we
        # never deliver more than the cap to downstream consumers. Bytes
        # beyond the cap are silently dropped (the downstream parse will
        # then fail with a clean validation error rather than OOM).
        original_receive = request.receive
        bytes_delivered = 0
        truncated = False
        # When the cap is exceeded, we keep draining original_receive() so
        # the ASGI server's keep-alive socket buffer is not poisoned for the
        # next request on the same connection
        # (adversarial review F-no-body-size-limit C1).
        drain_after_truncate = False
        already_drained = False

        async def _capped_receive() -> dict[str, object]:
            nonlocal bytes_delivered, truncated, drain_after_truncate, already_drained
            message = await original_receive()
            if message.get("type") != "http.request":
                return message
            body = message.get("body", b"") or b""
            if not body:
                return message
            if drain_after_truncate and not already_drained:
                # Drop the body but keep draining until more_body=False so
                # the ASGI server's receive buffer is exhausted and the next
                # request on this connection can be parsed cleanly.
                already_drained = not message.get("more_body", False)
                return {**message, "body": b"", "more_body": False}
            remaining = _MAX_REQUEST_BODY_BYTES - bytes_delivered
            if remaining <= 0:
                truncated = True
                drain_after_truncate = True
                already_drained = not message.get("more_body", False)
                return {**message, "body": b"", "more_body": False}
            if len(body) > remaining:
                truncated = True
                drain_after_truncate = True
                already_drained = not message.get("more_body", False)
                # Preserve the original message keys (per-chunk ``headers``
                # for HTTP/1.1 trailers) rather than ``dict(message)`` which
                # would drop them (adversarial review F-no-body-size-limit C3).
                return {**message, "body": body[:remaining], "more_body": False}
            bytes_delivered += len(body)
            return message

        request._receive = _capped_receive  # type: ignore[attr-defined]
        response = await call_next(request)
        # Truncated requests must NOT be allowed to keep-alive the
        # connection: the underlying socket buffer may still hold data the
        # server expects the app to consume, and reusing the connection
        # would either hang or corrupt the next request (C1 / C4).
        if truncated:
            response.headers["Connection"] = "close"
        return response

    # Include modular routers
    health_router, readiness_state = create_health_router()
    app.include_router(health_router)

    app.include_router(create_capabilities_router(settings))
    app.include_router(create_agent_hints_router(settings))
    app.include_router(create_well_known_router(settings))
    from .api.jobs_router import router as jobs_router
    from .api.map_router import router as map_router
    from .api.metrics_router import router as metrics_router
    from .api.render_router import router as render_router

    app.include_router(metrics_router)
    app.include_router(render_router)
    app.include_router(map_router)
    app.include_router(jobs_router)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Standardize on the documented CrawlerError shape and strip pydantic
        # error internals (input/ctx echo caller-supplied values back at them).
        sanitized = [{"loc": err.get("loc"), "msg": err.get("msg")} for err in exc.errors()]
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": sanitized},
            },
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        # Unwrap FastAPI's {"detail": ...} envelope so every error body matches
        # the documented CrawlerError model ({code, message, details}).
        detail = getattr(exc, "detail", None)
        headers = getattr(exc, "headers", None)
        if isinstance(detail, dict) and "code" in detail and "message" in detail:
            content = {
                "code": str(detail.get("code")),
                "message": str(detail.get("message")),
                "details": detail.get("details") or {},
            }
        else:
            content = {
                "code": f"http_{exc.status_code}",
                "message": str(detail) if detail is not None else "Request failed",
                "details": {},
            }
        return JSONResponse(status_code=exc.status_code, content=content, headers=headers)

    @app.post(
        "/v1/crawl",
        response_model=None,
        tags=["Crawler"],
        summary="Execute a policy-driven crawl",
        description=(
            "Policy is the caller-controlled execution plan for one URL or a batch of URLs. "
            "Use policy.strategies to choose the ordered fallback chain; strategies run in order, the service records "
            "every attempted strategy, and fallback stops at the first successful content result. Available strategy "
            "options (fetch strategies, tried in order) are HTTP_DECLARED_CRAWLER for transparent direct HTTP, "
            "HTTP_CFFI_IMPERSONATE for TLS-fingerprint-impersonated HTTP via curl_cffi with browser-like headers, "
            "BROWSER_HEADLESS for headless Firefox rendering via Camoufox, and BROWSER_HEADED_WARMED for headed Firefox "
            "with warmup URLs. How retrieved content is parsed is controlled separately by policy.extraction (full, "
            "trafilatura, or document); extraction is orthogonal to the fetch strategy. "
            "Returns Firecrawl-style condensed Markdown output with metadata for LLM-friendly consumption. "
            "Use policy.limits for timeout_seconds, max_retries, challenge_wait_seconds, "
            "and max_discovered_links; omit limits to use defaults from /v1/capabilities. Use request_profile for "
            "direct HTTP compatibility options such as user_agent_family, accept_language, fetch metadata, and benign "
            "per-host cookies. Use browser_profile for browser state, viewport, and resource_mode. Use discovery for "
            "robots, sitemaps, and feeds diagnostics. The service records per-URL provenance, selected strategy, raw "
            "evidence, classification evidence, quality score, bounded discovered links, diagnostics.frames for "
            "legacy frameset/iframe URLs, diagnostics.forms for form metadata without submitting forms, and stable "
            "error details. It reports discovered links but does not recursively enqueue them; callers must preserve "
            "form/search state before enqueueing pagination URLs. It classifies denial/challenge outcomes without "
            "CAPTCHA solving, proxy or identity rotation, credential injection, or bypass behavior."
        ),
        responses={
            200: {
                "model": UnifiedCrawlResponse,
                "description": "Successful crawl results (JSON output format).",
            },
            202: {
                "model": CrawlerError,
                "description": "Accepted for async webhook delivery; check status_url.",
            },
            422: {
                "model": CrawlerError,
                "description": "Invalid crawl request or unsupported output format.",
            },
        },
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "browser_fallback": {
                                "summary": "Direct HTTP with browser fallback",
                                "value": {
                                    "url": "https://records.example.gov/search?q=permit",
                                    "policy": BROWSER_FALLBACK_POLICY,
                                },
                            },
                            "document_extraction": {
                                "summary": "Document extraction policy",
                                "value": {
                                    "urls": ["https://records.example.gov/agenda.pdf"],
                                    "policy": DOCUMENT_EXTRACTION_POLICY,
                                },
                            },
                        }
                    }
                }
            }
        },
    )
    async def crawl(
        background_tasks: BackgroundTasks,
        payload: CrawlRequest = Body(
            ...,
            examples=[
                {
                    "url": "https://records.example.gov/search?q=permit",
                    "policy": BROWSER_FALLBACK_POLICY,
                }
            ],
        ),
    ) -> UnifiedCrawlResponse:
        readiness_state.begin_request()
        try:
            # Reject unsupported output formats BEFORE burning crawl resources
            # (adversarial review: validation used to happen only after the crawl).
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
            # If async_mode is set (or webhook_url provided for backward compat),
            # create a single-shot job and run in background.
            if async_mode or webhook_url:
                if webhook_url and (
                    not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://"))
                ):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "validation_error", "message": "Invalid webhook_url", "details": {}},
                    )
                if webhook_url:
                    _validate_webhook_url(webhook_url)
                # Use a stable request_id (also used as job_id for single-shot)
                request_id = f"crawl_{uuid.uuid4().hex[:12]}"
                # Create single-shot job in job_store (durable, resumable)
                job_store.create_single_shot(request_id, payload_dict.get("url", ""), payload_dict)
                # Dispatch background task
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
            # Synchronous execution (default)
            _crawl_started = time.monotonic()
            result = await _execute_crawl(payload_dict, settings=settings, shutdown_event=shutdown_event)
            get_metrics().observe_crawl(
                results=result.get("results", []),
                elapsed_ms=(time.monotonic() - _crawl_started) * 1000,
            )
            logger.info("crawl_request_completed", extra={"url_count": len(result.get("results", []))})
            unified = [_build_unified_result(item) for item in result.get("results", [])]
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
                # #17: emit the results in the requested wire format instead of the
                # default UnifiedCrawlResponse model.
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

    @app.get(
        "/v1/crawl/{request_id}",
        summary="Retrieve a completed crawl response",
        description=(
            "Return the stored UnifiedCrawlResponse for a previously completed "
            "/v1/crawl request (see status_url in the 202 webhook response). "
            "For async single-shot jobs, reads from the durable job store. "
            "Results are retained for BROWSER_CRAWLER_RESULT_TTL_SECONDS "
            "(default 1h, capped at BROWSER_CRAWLER_RESULT_MAX_ENTRIES entries)."
        ),
    )
    async def get_crawl_result(request_id: str) -> UnifiedCrawlResponse:
        # First check result_store (sync crawls)
        stored = result_store.get(request_id)
        if stored is not None:
            return UnifiedCrawlResponse(**stored)
        # Then check job_store for async single-shot jobs
        job = job_store.load(request_id)
        if job is not None and job.kind == JobKind.SINGLE_SHOT:
            if job.results:
                return UnifiedCrawlResponse(**job.results[0])
            # Job exists but not yet completed
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

else:
    app = None
