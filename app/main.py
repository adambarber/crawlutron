from __future__ import annotations

import asyncio
import logging
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any

from local_service_logging import configure_service_logging

from .api_models import (
    API_VERSION,
    POLICY_SCHEMA_VERSION,
    AgentHintsResponse,
    CapabilitiesResponse,
    UnifiedCrawlResponse,
    UnifiedCrawlResult,
    CrawlClassification,
    CrawlerError,
    CrawlRequest,
    CrawlStrategy,
    JobControlRequest,
    JobCreateRequest,
    JobCreateResponse,
    JobListResponse,
    JobStatusResponse,
    MapRequest,
    RenderRequest,
    DiscoveryResponse,
    FirecrawlMetadata,
    ReadinessResponse,
)
from .api.main_helpers import _build_job_status, _raise_not_found
from .api.policy_defaults import BROWSER_FALLBACK_POLICY, DOCUMENT_EXTRACTION_POLICY
from app.constants import (
    MAX_DISCOVERED_LINKS,
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)
from .crawl.strategies.browser import BrowserRenderer
from .crawl.strategies.browser_pool import PooledBrowserWorker, initialize_browser_pool, close_browser_pool, get_browser_pool
from .crawl.firecrawl_format import _extract_firecrawl_metadata
from .service import crawl_request, render_request
from .output_formats import serialize_results
from .map import build_site_map
from .crawl.job_store import JobStore, JobStatus
from .crawl.result_store import CrawlResultStore
from .settings import CrawlerSettings
from .metrics import get_metrics

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
from .health.readiness import create_health_router
from .discovery.capabilities import create_capabilities_router
from .discovery.agent_hints import create_agent_hints_router
from .discovery.well_known import create_well_known_router


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

async def _execute_crawl(payload_dict: dict) -> dict:
    """Shared crawl execution for /v1/crawl and /v1/map."""
    pooled_worker = PooledBrowserWorker(get_browser_pool())
    return await crawl_request(
        payload_dict,
        settings=settings,
        browser_worker=pooled_worker,
        shutdown_event=shutdown_event,
    )


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

def _format_crawl_item(item: dict[str, Any]) -> tuple[str | None, dict[str, Any]]:
    """Unified output formatter: produce ``(markdown, metadata_dict)`` for any result.

    Every access strategy (HTTP, browser, document/PDF, Trafilatura) funnels through
    this single path. The ``markdown`` field is populated by the unified extraction
    pipeline (trafilatura for web HTML, markitdown for documents, pass-through for plain
    text). For HTML pages the HTML metadata is extracted from the raw markup; results
    without HTML fall back to the strategy's extracted ``text`` so the consumer always
    receives a consistent Markdown payload regardless of how the content was acquired.
    """
    url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
    html = item.get("html")
    markdown = item.get("markdown")
    if markdown:
        if html and url:
            return markdown, _extract_firecrawl_metadata(html, url)
        return markdown, {}
    text = item.get("text")
    if not text:
        return None, {}
    return text, {}


def _build_unified_result(item: dict[str, Any]) -> UnifiedCrawlResult:
    """Map an internal engine result into the unified API response model.

    The unified response carries both the LLM-friendly ``markdown`` and the
    full ``diagnostics`` payload. Firecrawl-style metadata is produced by the
    shared Firecrawl metadata extractor (``extract_firecrawl_format``) from the
    raw HTML when available.
    """
    markdown, metadata_dict = _format_crawl_item(item)
    url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")
    metadata = FirecrawlMetadata(
        **{key: value for key, value in metadata_dict.items() if key in FirecrawlMetadata.model_fields}
    )
    if metadata.source_url is None:
        metadata.source_url = url
    return UnifiedCrawlResult(
        original_url=item["original_url"],
        normalized_url=item.get("normalized_url"),
        final_url=item.get("final_url"),
        status=item["status"],
        classification=item["classification"],
        selected_strategy=item.get("selected_strategy"),
        attempted_strategies=item.get("attempted_strategies", []),
        error=item.get("error"),
        markdown=markdown,
        metadata=metadata,
        html=item.get("html"),
        text=item.get("text"),
        quality_score=item.get("quality_score", 0.0),
        classification_evidence=item.get("classification_evidence", []),
        http_status=item.get("http_status"),
        headers=item.get("headers", {}),
        content_type=item.get("content_type"),
        extraction_method=item.get("extraction_method"),
        diagnostics=item.get("diagnostics", {}),
        discovered_link_count=item.get("discovered_link_count", 0),
        discovered_links_truncated=item.get("discovered_links_truncated", False),
        discovered_links=item.get("discovered_links", []),
        next_page_url=item.get("next_page_url"),
        validators=item.get("validators") or None,
    )


def _render_result_to_unified(item: dict[str, Any]) -> dict[str, Any]:
    """Adapt a browser ``CrawlResult`` dict into the unified result shape.

    ``/v1/render`` reuses the same browser worker as the universal crawl path,
    so its output is normalized through the identical ``_build_unified_result``
    pipeline. Browser-specific discovery (intercepted XHR, screenshots) lives in
    the result ``metadata`` dict and is carried into ``diagnostics`` so the
    unified schema stays the single source of truth for both endpoints.
    """
    metadata = item.get("metadata") or {}
    diagnostics: dict[str, Any] = {
        key: value
        for key, value in metadata.items()
        if key not in {"title", "content_length", "browser_reused", "challenge_resolved", "extraction_method"}
    }
    diagnostics.setdefault("browser_profile", {})
    return {
        "original_url": item["url"],
        "normalized_url": item["url"],
        "final_url": item.get("final_url"),
        "status": item["status"],
        "classification": item["classification"],
        "selected_strategy": None,
        "attempted_strategies": [
            {
                "strategy": "BROWSER_HEADLESS",
                "status": item["status"],
                "classification": item["classification"],
                "http_status": None,
                "content_type": None,
                "elapsed_ms": int(item.get("elapsed_ms", 0)),
                "error": item.get("error"),
            }
        ],
        "error": item.get("error"),
        "markdown": None,
        "metadata": {},
        "html": item.get("html"),
        "text": item.get("text"),
        "quality_score": 1.0 if item["status"] == "success" else 0.0,
        "classification_evidence": [],
        "http_status": None,
        "headers": {},
        "content_type": None,
        "extraction_method": metadata.get("extraction_method"),
        "diagnostics": diagnostics,
        "discovered_link_count": 0,
        "discovered_links_truncated": False,
        "discovered_links": [],
        "next_page_url": None,
    }


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
    def _lifespan(app: FastAPI):
        @asynccontextmanager
        async def lifespan_context(_app: FastAPI):
            # Startup: initialize browser pool, load cookie jar if persistence enabled
            await initialize_browser_pool(settings)
            if settings.cookie_persistence_enabled:
                from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR
                _GLOBAL_COOKIE_JAR.load_from_file(settings.cookie_jar_path)

            yield

            # Shutdown: save cookie jar if persistence enabled, then close clients and browser pool
            if settings.cookie_persistence_enabled:
                from app.crawl.strategies.http import _GLOBAL_COOKIE_JAR
                _GLOBAL_COOKIE_JAR.save_to_file(settings.cookie_jar_path)

            from app.crawl.strategies.http import close_async_http_client

            shutdown_event.set()
            await close_async_http_client()
            await close_browser_pool()

        return lifespan_context

    app = FastAPI(
        title="Browser Crawler Execution Service",
        lifespan=_lifespan(None),
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
        contact={"name": "dev-stack local services"},
        license_info={"name": "Local development service"},
        openapi_tags=[
            {"name": "Health", "description": "Lightweight liveness and crawl-capacity readiness endpoints for local orchestration."},
            {"name": "Discovery", "description": "Machine-readable service capabilities and AI agent guidance."},
            {"name": "Crawler", "description": "Browser render and universal policy-driven crawl execution endpoints."},
        ],
    )

    # Include modular routers
    health_router, readiness_state = create_health_router()
    app.include_router(health_router)

    app.include_router(create_capabilities_router(settings))
    app.include_router(create_agent_hints_router(settings))
    app.include_router(create_well_known_router(settings))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Standardize on the documented CrawlerError shape and strip pydantic
        # error internals (input/ctx echo caller-supplied values back at them).
        sanitized = [
            {"loc": err.get("loc"), "msg": err.get("msg")}
            for err in exc.errors()
        ]
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
        "/v1/render",
        tags=["Crawler"],
        summary="Render pages with the browser worker",
        description="Backward-compatible browser-render endpoint. Prefer POST /v1/crawl for policy-driven universal crawling.",
        responses={400: {"model": CrawlerError, "description": "Invalid render request."}},
    )
    async def render(payload: RenderRequest) -> UnifiedCrawlResponse:
        readiness_state.begin_request()
        try:
            pooled_worker = PooledBrowserWorker(get_browser_pool())
            result = await render_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                worker=pooled_worker,
            )
            logger.info("render_request_completed", extra={"url_count": len(result.get("results", []))})
            return UnifiedCrawlResponse(
                request_id=result["request_id"],
                submitted_count=result["submitted_count"],
                policy_schema_version=POLICY_SCHEMA_VERSION,
                success=True,
                results=[_build_unified_result(_render_result_to_unified(item)) for item in result.get("results", [])],
            )
        except ValueError as error:
            logger.warning("render_request_rejected", extra={"error": str(error)})
            raise HTTPException(status_code=400, detail={"code": str(error), "message": str(error), "details": {}}) from error
        finally:
            readiness_state.end_request()

    # Bound webhook-mode background crawls: without a cap, a flood of 202s
    # spawns unbounded concurrent crawls until the container OOMs.
    _webhook_crawl_semaphore = asyncio.Semaphore(
        max(1, int(os.environ.get("BROWSER_CRAWLER_MAX_WEBHOOK_CONCURRENCY", "4")))
    )

    async def _deliver_webhook(webhook_url: str, request_id: str, payload_dict: dict[str, Any], crawl_settings: CrawlerSettings) -> None:
        """Run the crawl in the background and POST the result to ``webhook_url`` (#15).

        Best-effort delivery: failures are swallowed so a dead callback never
        crashes the crawler worker. Concurrency is capped by
        ``BROWSER_CRAWLER_MAX_WEBHOOK_CONCURRENCY``; excess deliveries wait on
        the semaphore instead of multiplying resource usage.
        """
        async with _webhook_crawl_semaphore:
            await _run_and_deliver_webhook(webhook_url, request_id, payload_dict, crawl_settings)

    async def _run_and_deliver_webhook(webhook_url: str, request_id: str, payload_dict: dict[str, Any], crawl_settings: CrawlerSettings) -> None:
        # SSRF protection for webhook URL (#P0)
        _validate_webhook_url(webhook_url)
        pooled_worker = PooledBrowserWorker(get_browser_pool())
        result = await crawl_request(
            payload_dict,
            settings=crawl_settings,
            browser_worker=pooled_worker,
            shutdown_event=shutdown_event,
        )
        result["request_id"] = request_id
        get_metrics().observe_crawl(results=result.get("results", []))
        unified_results = [_build_unified_result(item) for item in result.get("results", [])]
        response_payload = UnifiedCrawlResponse(
            request_id=request_id,
            submitted_count=result.get("submitted_count", len(unified_results)),
            policy_schema_version=result.get("policy_schema_version", ""),
            success=True,
            results=unified_results,
        ).model_dump()
        try:
            import httpx
            import json
            import os

            # Limit webhook payload size to prevent OOM/DoS (default 10MB)
            max_webhook_size = int(os.environ.get("BROWSER_CRAWLER_MAX_WEBHOOK_SIZE", "10485760"))
            payload_bytes = json.dumps(response_payload, separators=(",", ":")).encode("utf-8")
            if len(payload_bytes) > max_webhook_size:
                logger.warning("webhook_payload_too_large", extra={"size": len(payload_bytes), "limit": max_webhook_size})
                return

            # DNS-rebinding-safe delivery: resolve once and pin the connection to
            # that exact IP, preserving the original Host header so virtual-host
            # routing still works (#3).
            from urllib.parse import urlparse as _urlparse
            from app.crawl.strategies.http import _resolve_and_pin_host

            _hostname, pinned_ip = _resolve_and_pin_host(webhook_url)
            _parsed = _urlparse(webhook_url)
            pinned_url = (
                f"{_parsed.scheme}://{pinned_ip}:{_parsed.port or (80 if _parsed.scheme == 'http' else 443)}"
                f"{_parsed.path or '/'}"
                + (f"?{_parsed.query}" if _parsed.query else "")
            )
            request_headers = {"Content-Type": "application/json"}
            if _parsed.hostname:
                request_headers["Host"] = _parsed.hostname
            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(pinned_url, content=payload_bytes, headers=request_headers)
        except Exception:  # webhook delivery is best-effort
            pass

    def _validate_webhook_url(webhook_url: str) -> None:
        """Validate webhook URL to prevent SSRF attacks.

        Similar to crawl target validation but for outbound webhook callbacks.
        Resolves the hostname and rejects any that map to a private/loopback
        address, protecting against attacker-controlled DNS that returns an
        internal IP (DNS-rebinding class of SSRF).
        """
        import os
        from urllib.parse import urlparse
        from app.crawl.strategies.http import _check_ssrf_protection

        allow_private = os.environ.get("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS") == "1"
        parsed = urlparse(webhook_url)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("webhook_url must use http or https scheme")
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("webhook_url missing hostname")
        if allow_private:
            return
        if hostname in {"localhost", "host.docker.internal"}:
            raise ValueError("webhook_url points to private network target")
        # Resolve the hostname and reject private/loopback IPs (DNS-rebinding safe).
        _check_ssrf_protection(webhook_url)

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
            200: {"model": UnifiedCrawlResponse, "description": "Successful crawl results (JSON output format)."},
            202: {"model": CrawlerError, "description": "Accepted for async webhook delivery; check status_url."},
            422: {"model": CrawlerError, "description": "Invalid crawl request or unsupported output format."},
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
            if webhook_url:
                # #15: accept immediately (202) and deliver the result out-of-band.
                if not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://")):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "validation_error", "message": "Invalid webhook_url", "details": {}},
                    )
                # Validate the webhook URL at accept time: failing silently in
                # the background task means an accepted crawl is never delivered.
                _validate_webhook_url(webhook_url)
                request_id = f"crawl_{uuid.uuid4().hex[:12]}"
                background_tasks.add_task(
                    _deliver_webhook, webhook_url, request_id, payload_dict, settings
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
            result = await _execute_crawl(payload_dict)
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
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Request validation failed", "details": {"errors": [{"msg": str(error)}]}}) from error
        finally:
            readiness_state.end_request()
    @app.post(
        "/v1/map",
        tags=["Crawler"],
        summary="Discover the URL frontier for a root URL (#2)",
        description=(
            "Map a root URL to the URLs reachable from it: in-page discovered links plus the "
            "URLs advertised via robots.txt sitemaps and feeds. Discovery is always enabled; the "
            "caller controls the fetch policy (strategies, limits, scope_regex). The service returns "
            "the de-duplicated frontier but does not recursively enqueue it; callers own recursion, "
            "dedupe, persistence, and host-policy decisions."
        ),
        responses={400: {"model": CrawlerError, "description": "Invalid map request."}},
    )
    async def map_site(payload: MapRequest) -> dict:
        readiness_state.begin_request()
        try:
            payload_dict = payload.model_dump(exclude_none=True)
            # #2: mapping is a discovery operation; force discovery on regardless of caller input.
            policy = payload_dict.setdefault("policy", {})
            disc = dict(policy.get("discovery") or {})
            disc["robots"] = True
            disc["sitemaps"] = True
            disc["feeds"] = True
            policy["discovery"] = disc
            limits = dict(policy.get("limits") or {})
            limits.setdefault("max_discovered_links", MAX_DISCOVERED_LINKS)
            policy["limits"] = limits
            _crawl_started = time.monotonic()
            result = await _execute_crawl(payload_dict)
            get_metrics().observe_crawl(
                results=result.get("results", []),
                elapsed_ms=(time.monotonic() - _crawl_started) * 1000,
            )
            logger.info("map_request_completed", extra={"url_count": len(result.get("results", []))})
            return build_site_map(result)
        except ValueError as error:
            logger.warning("map_request_rejected", extra={"error": str(error)})
            raise HTTPException(
                status_code=422,
                detail={"code": "validation_error", "message": "Request validation failed", "details": {"errors": [{"msg": str(error)}]}},
            ) from error
        finally:
            readiness_state.end_request()


    @app.get(
        "/v1/crawl/{request_id}",
        summary="Retrieve a completed crawl response",
        description=(
            "Return the stored UnifiedCrawlResponse for a previously completed "
            "/v1/crawl request (see status_url in the 202 webhook response). "
            "Results are retained for BROWSER_CRAWLER_RESULT_TTL_SECONDS "
            "(default 1h, capped at BROWSER_CRAWLER_RESULT_MAX_ENTRIES entries)."
        ),
    )
    async def get_crawl_result(request_id: str) -> UnifiedCrawlResponse:
        stored = result_store.get(request_id)
        if stored is None:
            raise HTTPException(
                status_code=404,
                detail={"code": "not_found", "message": f"Unknown or expired request_id: {request_id}", "details": {}},
            )
        return UnifiedCrawlResponse(**stored)

    @app.post(
        "/v1/jobs",
        tags=["Crawler"],
        summary="Create a new resumable whole-site crawl job (#3+#14)",
        description=(
            "Create a crawl job that recursively discovers and crawls URLs from a root URL. "
            "The job persists its state (frontier, visited, results) to disk and can be "
            "paused, resumed, and inspected across restarts. Callers own the job lifecycle."
        ),
        responses={422: {"model": CrawlerError, "description": "Invalid job request."}},
    )
    async def create_job(payload: JobCreateRequest) -> JobCreateResponse:
        job = job_store.create(payload.root_url, payload.policy.model_dump(exclude_none=True))
        return JobCreateResponse(
            job_id=job.job_id,
            status=job.status.value,
            root_url=job.root_url,
            created_at=job.created_at,
        )

    @app.get(
        "/v1/jobs",
        tags=["Crawler"],
        summary="List all crawl jobs (#3+#14)",
        description="Return a list of all crawl jobs with basic status info.",
    )
    async def list_jobs() -> JobListResponse:
        jobs = job_store.list_jobs()
        return JobListResponse(
            jobs=[JobStatusResponse(**j) for j in jobs]
        )

    @app.get(
        "/v1/jobs/{job_id}",
        tags=["Crawler"],
        summary="Get crawl job status and results (#3+#14)",
        description="Return the full job state including frontier, visited URLs, and results.",
        responses={404: {"model": CrawlerError, "description": "Job not found."}},
    )
    async def get_job(job_id: str) -> JobStatusResponse:
        job = job_store.load(job_id)
        if job is None:
            _raise_not_found(job_id)
        return _build_job_status(job)

    @app.post(
        "/v1/jobs/{job_id}/control",
        tags=["Crawler"],
        summary="Control a crawl job (pause/resume/cancel) (#3+#14)",
        description="Pause, resume, or cancel a running crawl job.",
        responses={404: {"model": CrawlerError, "description": "Job not found."},
                   422: {"model": CrawlerError, "description": "Invalid action."}},
    )
    async def control_job(job_id: str, payload: JobControlRequest) -> JobStatusResponse:
        job = job_store.load(job_id)
        if job is None:
            _raise_not_found(job_id)

        action = payload.action.lower()
        if action == "pause":
            if job.status != JobStatus.RUNNING:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_state", "message": "Can only pause a running job"},
                )
            job.status = JobStatus.PAUSED
        elif action == "resume":
            if job.status != JobStatus.PAUSED:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_state", "message": "Can only resume a paused job"},
                )
            job.status = JobStatus.RUNNING
        elif action == "cancel":
            if job.status in {JobStatus.COMPLETED, JobStatus.CANCELLED, JobStatus.FAILED}:
                raise HTTPException(
                    status_code=422,
                    detail={"code": "invalid_state", "message": "Job already terminal"},
                )
            job.status = JobStatus.CANCELLED
        else:
            raise HTTPException(
                status_code=422,
                detail={"code": "invalid_action", "message": f"Unknown action: {action}"},
            )

        job_store.save(job)
        return _build_job_status(job)

    @app.delete(
        "/v1/jobs/{job_id}",
        tags=["Crawler"],
        summary="Delete a crawl job (#3+#14)",
        description="Permanently remove a crawl job and its data.",
        responses={404: {"model": CrawlerError, "description": "Job not found."}},
    )
    async def delete_job(job_id: str) -> dict[str, str]:
        if not job_store.delete(job_id):
            _raise_not_found(job_id)
        return {"status": "deleted", "job_id": job_id}

    @app.get("/metrics", tags=["Health"], summary="Prometheus metrics endpoint (#16)")
    async def metrics_endpoint() -> "Response":
        return Response(get_metrics().as_prometheus(), media_type="text/plain; version=0.0.4")

else:
    app = None
