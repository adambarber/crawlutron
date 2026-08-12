from __future__ import annotations

import asyncio
import logging
from typing import Any

from local_service_logging import configure_service_logging

from .api_models import (
    API_VERSION,
    POLICY_SCHEMA_VERSION,
    AgentHintsResponse,
    CapabilitiesResponse,
    CondensedCrawlResponse,
    CrawlClassification,
    CrawlerError,
    CrawlRequest,
    CrawlStrategy,
    DiscoveryResponse,
    FirecrawlMetadata,
    ReadinessResponse,
)
from .browser import BrowserRenderer
from .crawl.firecrawl_format import _extract_firecrawl_metadata
from .service import crawl_request, render_request
from .settings import CrawlerSettings

try:
    from fastapi import Body, FastAPI, HTTPException, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover - imported in container runtime
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


def build_condensed_crawl_response(results: list[dict[str, Any]]) -> CondensedCrawlResponse:
    """Transform internal crawl results into the Firecrawl-style condensed response."""
    condensed_data: list[dict[str, Any]] = []
    for item in results:
        markdown, metadata_dict = _format_crawl_item(item)
        url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")

        metadata = FirecrawlMetadata(
            title=metadata_dict.get("title"),
            description=metadata_dict.get("description"),
            language=metadata_dict.get("language"),
            og_title=metadata_dict.get("og_title"),
            og_description=metadata_dict.get("og_description"),
            og_image=metadata_dict.get("og_image"),
            og_url=metadata_dict.get("og_url"),
            og_type=metadata_dict.get("og_type"),
            og_site_name=metadata_dict.get("og_site_name"),
            twitter_card=metadata_dict.get("twitter_card"),
            twitter_title=metadata_dict.get("twitter_title"),
            twitter_description=metadata_dict.get("twitter_description"),
            twitter_image=metadata_dict.get("twitter_image"),
            canonical_url=metadata_dict.get("canonical_url"),
            favicon=metadata_dict.get("favicon"),
            status_code=item.get("http_status"),
            content_type=item.get("content_type"),
            source_url=url,
        )

        condensed_data.append({
            "url": url,
            "markdown": markdown,
            "metadata": metadata,
            "warning": item.get("classification") if item.get("classification") != "rendered" else None,
            "error": item.get("error"),
        })

    return CondensedCrawlResponse(success=True, data=condensed_data)


if FastAPI is not None:
    app = FastAPI(
        title="Browser Crawler Execution Service",
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
    async def render(payload: dict[str, Any]) -> dict[str, Any]:
        readiness_state.begin_request()
        try:
            result = await render_request(payload, settings=settings, worker=lambda job: renderer.render(job.url, job.options))  # pragma: no cover - requires browser
            logger.info("render_request_completed", extra={"url_count": len(result.get("results", []))})  # pragma: no cover - requires browser
            return result  # pragma: no cover - requires browser
        except ValueError as error:
            logger.warning("render_request_rejected", extra={"error": str(error)})
            raise HTTPException(status_code=400, detail={"code": str(error), "message": str(error), "details": {}}) from error
        finally:
            readiness_state.end_request()

    @app.post(
        "/v1/crawl",
        response_model=CondensedCrawlResponse,
        tags=["Crawler"],
        summary="Execute a policy-driven crawl",
        description=(
            "Policy is the caller-controlled execution plan for one URL or a batch of URLs. "
            "Use policy.strategies to choose the ordered fallback chain; strategies run in order, the service records "
            "every attempted strategy, and fallback stops at the first successful content result. Available strategy "
            "options are HTTP_DECLARED_CRAWLER for transparent direct HTTP, HTTP_SITE_COMPAT for browser-like direct "
            "HTTP headers, HTTP_CFFI_IMPERSONATE for TLS-fingerprint-impersonated HTTP via curl_cffi, BROWSER_HEADLESS "
            "for headless Firefox rendering via Camoufox, BROWSER_HEADED_WARMED for headed Firefox with warmup URLs, "
            "DOCUMENT_EXTRACT for PDF/non-HTML text extraction, and TRAFILATURA_EXTRACT for HTML main-text extraction only. "
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
                                            "HTTP_DECLARED_CRAWLER",
                                            "HTTP_SITE_COMPAT",
                                            "HTTP_CFFI_IMPERSONATE",
                                            "BROWSER_HEADLESS",
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
                                        "strategies": ["DOCUMENT_EXTRACT"],
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
        payload: CrawlRequest = Body(
            ...,
            examples=[
                {
                    "url": "https://records.example.gov/search?q=permit",
                    "policy": {
                        "strategies": [
                            "HTTP_DECLARED_CRAWLER",
                            "HTTP_SITE_COMPAT",
                            "HTTP_CFFI_IMPERSONATE",
                            "BROWSER_HEADLESS",
                        ],
                        "limits": {
                            "timeout_seconds": 30,
                            "max_retries": 2,
                            "max_discovered_links": 250,
                        },
                    },
                }
            ],
        )
    ) -> CondensedCrawlResponse:
        readiness_state.begin_request()
        try:
            result = await crawl_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                browser_worker=lambda job: renderer.render(job.url, job.options),
            )
            logger.info("crawl_request_completed", extra={"url_count": len(result.get("results", []))})
            return build_condensed_crawl_response(result.get("results", []))
        except ValueError as error:
            logger.warning("crawl_request_rejected", extra={"error": str(error)})
            raise HTTPException(status_code=422, detail={"code": "validation_error", "message": "Request validation failed", "details": {"errors": [{"msg": str(error)}]}}) from error
        finally:
            readiness_state.end_request()
else:  # pragma: no cover - FastAPI unavailable at runtime
    app = None
