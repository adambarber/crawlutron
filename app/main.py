from __future__ import annotations

import asyncio
import logging
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
    RenderRequest,
    DiscoveryResponse,
    FirecrawlMetadata,
    ReadinessResponse,
)
from app.constants import (
    BROWSER_HEADLESS,
    EXTRACTION_DOCUMENT,
    EXTRACTION_FULL,
    EXTRACTION_TRAFILATURA,
    HTTP_CFFI_IMPERSONATE,
    HTTP_DECLARED_CRAWLER,
)
from .crawl.strategies.browser import BrowserRenderer
from .crawl.firecrawl_format import _extract_firecrawl_metadata
from .service import crawl_request, render_request
from .output_formats import serialize_results
from .settings import CrawlerSettings
from .metrics import metrics

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
logger = configure_service_logging(
    service_name="browser-crawler",
    log_level=settings.log_level,
    log_file_path=settings.log_file_path,
    log_file_max_bytes=settings.log_file_max_bytes,
    log_file_backup_count=settings.log_file_backup_count,
)
renderer = BrowserRenderer()
shutdown_event = asyncio.Event()
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.setLevel(logger.level)

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
        async def lifespan_context(allow_concurrency: bool, *args, **kwargs):
            yield
            # Shutdown: signal graceful stop, then close shared HTTP client and browser renderer.
            from app.crawl.strategies.http import close_async_http_client

            shutdown_event.set()
            await close_async_http_client()
            await renderer.close()

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
    health_router, readiness_state = create_health_router(settings)
    app.include_router(health_router)

    app.include_router(create_capabilities_router(settings))
    app.include_router(create_agent_hints_router(settings))
    app.include_router(create_well_known_router(settings))

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": exc.errors()},
            },
        )

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
            result = await render_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                worker=lambda job: renderer.render(job.url, job.options),
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

    async def _deliver_webhook(webhook_url: str, payload_dict: dict[str, Any], crawl_settings: CrawlerSettings) -> None:
        """Run the crawl in the background and POST the result to ``webhook_url`` (#15).

        Best-effort delivery: failures are swallowed so a dead callback never
        crashes the crawler worker.
        """
        result = await crawl_request(
            payload_dict,
            settings=crawl_settings,
            browser_worker=lambda job: renderer.render(job.url, job.options),
            shutdown_event=shutdown_event,
        )
        metrics.observe_crawl(results=result.get("results", []))
        try:
            import httpx

            async with httpx.AsyncClient(timeout=30.0) as client:
                await client.post(webhook_url, json=result, headers={"Content-Type": "application/json"})
        except Exception:  # webhook delivery is best-effort
            pass

    @app.post(
        "/v1/crawl",
        response_model=UnifiedCrawlResponse,
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
        responses={400: {"model": CrawlerError, "description": "Invalid crawl request."}},
        openapi_extra={
            "requestBody": {
                "content": {
                    "application/json": {
                        "examples": {
                            "browser_fallback": {
                                "summary": "Direct HTTP with browser fallback",
                                "value": {
                                    "url": "https://records.example.gov/search?q=permit",
                                    "policy": {
                                        "strategies": [
                                            HTTP_DECLARED_CRAWLER,
                                            HTTP_CFFI_IMPERSONATE,
                                            BROWSER_HEADLESS,
                                        ],
                                        "limits": {
                                            "timeout_seconds": 30,
                                            "max_retries": 2,
                                            "max_discovered_links": 250,
                                        },
                                    },
                                },
                            },
                            "document_extraction": {
                                "summary": "Document extraction policy",
                                "value": {
                                    "urls": ["https://records.example.gov/agenda.pdf"],
                                    "policy": {
                                        "strategies": [HTTP_DECLARED_CRAWLER],
                                        "extraction": EXTRACTION_DOCUMENT,
                                        "limits": {
                                            "timeout_seconds": 60,
                                            "max_retries": 1,
                                            "max_discovered_links": 0,
                                        },
                                    },
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
                    "policy": {
                        "strategies": [
                            HTTP_DECLARED_CRAWLER,
                            HTTP_CFFI_IMPERSONATE,
                            BROWSER_HEADLESS,
                        ],
                        "limits": {
                            "timeout_seconds": 30,
                            "max_retries": 2,
                            "max_discovered_links": 250,
                        },
                    },
                }
            ],
        ),
    ) -> UnifiedCrawlResponse:
        readiness_state.begin_request()
        try:
            payload_dict = payload.model_dump(exclude_none=True)
            webhook_url = payload.webhook_url
            if webhook_url:
                # #15: accept immediately (202) and deliver the result out-of-band.
                if not isinstance(webhook_url, str) or not webhook_url.startswith(("http://", "https://")):
                    raise HTTPException(
                        status_code=422,
                        detail={"code": "validation_error", "message": "Invalid webhook_url", "details": {}},
                    )
                background_tasks.add_task(_deliver_webhook, webhook_url, payload_dict, settings)
                metrics.observe_crawl(accepted=True)
                request_id = f"crawl_{uuid.uuid4().hex[:12]}"
                return JSONResponse(
                    status_code=202,
                    content={
                        "status": "accepted",
                        "request_id": request_id,
                        "status_url": f"/v1/crawl/{request_id}",
                        "webhook_url": webhook_url,
                    },
                )
            result = await crawl_request(
                payload_dict,
                settings=settings,
                browser_worker=lambda job: renderer.render(job.url, job.options),
                shutdown_event=shutdown_event,
            )
            metrics.observe_crawl(results=result.get("results", []))
            logger.info("crawl_request_completed", extra={"url_count": len(result.get("results", []))})
            unified = [_build_unified_result(item) for item in result.get("results", [])]
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
            metrics.observe_crawl(errors=1)
            logger.warning("crawl_request_rejected", extra={"error": str(error)})
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Request validation failed", "details": {"errors": [{"msg": str(error)}]}}) from error
        finally:
            readiness_state.end_request()
    @app.get("/metrics", tags=["Health"], summary="Prometheus metrics endpoint (#16)")
    async def metrics_endpoint() -> "Response":
        return Response(metrics.as_prometheus(), media_type="text/plain; version=0.0.4")

else:
    app = None
