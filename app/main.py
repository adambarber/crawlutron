from __future__ import annotations

import asyncio
import logging
import threading
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
    CrawlResponse,
    CrawlStrategy,
    DiscoveryResponse,
    FirecrawlMetadata,
    ReadinessResponse,
)
from .service import extract_firecrawl_format
from .browser import BrowserRenderer
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


class ReadinessState:
    def __init__(self, *, max_active_requests: int) -> None:
        self._max_active_requests = max(1, max_active_requests)
        self._active_requests = 0
        self._lock = threading.Lock()

    def begin_request(self) -> None:
        with self._lock:
            self._active_requests += 1

    def end_request(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)

    def snapshot(self) -> dict[str, int | bool | str]:
        with self._lock:
            active_requests = self._active_requests
        saturated = active_requests >= self._max_active_requests
        return {
            "status": "saturated" if saturated else "ready",
            "active_requests": active_requests,
            "max_active_requests": self._max_active_requests,
            "saturated": saturated,
        }


settings = CrawlerSettings.from_mapping()
logger = configure_service_logging(
    service_name="browser-crawler",
    log_level=settings.log_level,
    log_file_path=settings.log_file_path,
    log_file_max_bytes=settings.log_file_max_bytes,
    log_file_backup_count=settings.log_file_backup_count,
)
renderer = BrowserRenderer()
readiness_state = ReadinessState(max_active_requests=settings.global_concurrency)
uvicorn_access_logger = logging.getLogger("uvicorn.access")
uvicorn_access_logger.setLevel(logger.level)

STRATEGY_DESCRIPTIONS = {
    CrawlStrategy.HTTP_DECLARED_CRAWLER: "Direct HTTP using a transparent crawler user agent and caller-approved headers.",
    CrawlStrategy.HTTP_SITE_COMPAT: "Direct HTTP using browser-like compatibility headers before escalating to browser rendering.",
    CrawlStrategy.HTTP_CFFI_IMPERSONATE: "TLS-fingerprint-impersonated HTTP via curl_cffi with chrome124 impersonation for WAF/Cloudflare bypass.",
    CrawlStrategy.BROWSER_HEADLESS: "Firefox rendering in headless mode using Camoufox for JavaScript-dependent pages.",
    CrawlStrategy.BROWSER_HEADED_WARMED: "Headed Firefox with configured warmup URLs for challenge-prone municipal systems.",
    CrawlStrategy.DOCUMENT_EXTRACT: "Document/body extraction for PDFs and non-HTML responses when text evidence is needed.",
    CrawlStrategy.TRAFILATURA_EXTRACT: "HTML-only main-text extraction strategy for noisy HTML. It does not parse PDF or binary documents; use DOCUMENT_EXTRACT for PDFs.",
}

OUTPUT_FIELDS = [
    "original_url",
    "normalized_url",
    "final_url",
    "status",
    "classification",
    "selected_strategy",
    "attempted_strategies",
    "http_status",
    "headers",
    "content_type",
    "html",
    "text",
    "extraction_method",
    "quality_score",
    "classification_evidence",
    "discovered_link_count",
    "discovered_links_truncated",
    "discovered_links",
    "diagnostics",
    "error",
]

EXAMPLE_POLICIES: dict[str, dict[str, Any]] = {
    "direct_transparent_crawler_http": {
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategies": ["HTTP_DECLARED_CRAWLER"],
        "limits": {"timeout_seconds": 20, "max_retries": 1},
    },
    "site_compatible_http": {
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategies": ["HTTP_SITE_COMPAT"],
        "limits": {"timeout_seconds": 20, "max_retries": 1},
    },
    "browser_fallback": {
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategies": ["HTTP_DECLARED_CRAWLER", "HTTP_SITE_COMPAT", "BROWSER_HEADLESS"],
        "limits": {"timeout_seconds": 30, "max_retries": 2, "max_discovered_links": 250},
        "request_profile": {"user_agent_family": "desktop_chrome", "accept_language": "en-US,en;q=0.9"},
        "browser_profile": {"state": "fresh", "viewport": "desktop", "resource_mode": "full"},
        "discovery": {"robots": True, "sitemaps": True, "feeds": True},
    },
    "headed_warmed_browser": {
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategies": ["HTTP_SITE_COMPAT", "BROWSER_HEADED_WARMED"],
        "limits": {"timeout_seconds": 45, "max_retries": 1, "challenge_wait_seconds": 20},
    },
    "document_extraction": {
        "schema_version": POLICY_SCHEMA_VERSION,
        "strategies": ["DOCUMENT_EXTRACT", "HTTP_DECLARED_CRAWLER"],
        "limits": {"timeout_seconds": 30, "max_retries": 1},
    },
}


def capabilities_payload() -> dict[str, Any]:
    return {
        "service": "browser-crawler",
        "version": API_VERSION,
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "supported_strategies": [strategy.value for strategy in CrawlStrategy],
        "supported_classifications": [classification.value for classification in CrawlClassification],
        "output_fields": OUTPUT_FIELDS,
        "default_limits": {
            "batch_size": settings.max_batch_size,
            "max_batch_size": settings.max_batch_size,
            "timeout_seconds": settings.default_timeout_seconds,
            "max_retries": settings.default_max_retries,
            "challenge_wait_seconds": 15,
            "max_discovered_links": 250,
            "global_concurrency": settings.global_concurrency,
            "per_domain_concurrency": settings.per_domain_concurrency,
            "per_domain_delay_ms": settings.per_domain_delay_ms,
        },
        "features": {
            "headed_browser": True,
            "pdf_extraction": True,
            "direct_http": True,
            "trafilatura": True,
            "discovered_links": True,
            "frame_discovery": True,
            "form_discovery": True,
            "recursive_frontier": False,
            "warmup": True,
            "request_profiles": True,
            "browser_profiles": True,
            "captcha_solving": False,
            "proxy_rotation": False,
            "identity_rotation": False,
            "credential_bypass": False,
            "cloudflare_bypass": True,
            "robots_diagnostics": True,
            "sitemap_discovery": True,
            "feed_discovery": True,
            "adaptive_retry": True,
            "runtime_diagnostics": True,
            "domain_aware_concurrency": True,
            "readiness_endpoint": True,
            "event_loop_thread_offload": True,
        },
        "openapi_url": "/openapi.json",
        "swagger_ui_url": "/docs",
    }


def agent_hints_payload() -> dict[str, Any]:
    return {
        "service": "browser-crawler",
        "strategies": {strategy.value: description for strategy, description in STRATEGY_DESCRIPTIONS.items()},
        "guidance": [
            "Use GET /health for lightweight process liveness and Docker healthchecks.",
            "Use GET /ready for crawl capacity/readiness; HTTP 503 means saturated backpressure, not necessarily crashed.",
            "Long direct HTTP, document extraction, Trafilatura, and synchronous browser work are offloaded from the FastAPI event loop so liveness can stay responsive during crawls.",
            "Prefer direct transparent crawler HTTP for sites that allow it.",
            "Use browser strategies only when direct HTTP is blocked, incomplete, or requires rendering.",
            "Do not treat HTTP 200 as content success without checking classification and quality score.",
            "Inspect classification_evidence before turning block classifications into host policy changes.",
            "Preserve raw evidence and provenance in downstream artifacts.",
            "Do not mutate host policy automatically from a crawl result.",
            "Use attempt history to generate reviewable policy suggestions.",
            "Treat discovered_links as bounded extraction output only; callers own frontier queueing, dedupe, and persistence.",
            "Inspect diagnostics.frames for legacy frameset and iframe URLs; the service reports frames but does not recursively crawl them.",
            "Inspect diagnostics.forms for form actions, methods, targets, and controls; the service reports forms but does not automatically submit them.",
            "Treat discovered pagination links as URL evidence only; callers must preserve form/search state before enqueueing paginated result URLs.",
            "Use discovered_link_count and discovered_links_truncated to decide whether to request a higher max_discovered_links limit.",
            "Do not bypass Cloudflare, WAF, CAPTCHA, login, paywall, geolocation, or access-denied controls; report those outcomes as classifications and evidence.",
            "Do not solve CAPTCHAs, do not rotate proxies, do not rotate identities, do not inject credentials, and do not continue after explicit denial.",
            "Passive challenge waiting only observes whether a public page resolves within the configured timeout; it must not click, solve, outsource, or evade challenges.",
            "Keep Chonkie downstream of extracted text rather than part of retrieval strategy selection.",
        ],
        "example_policies": EXAMPLE_POLICIES,
    }


def discovery_payload() -> dict[str, Any]:
    return {
        "service": "browser-crawler",
        "version": API_VERSION,
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "links": {
            "openapi": "/openapi.json",
            "swagger_ui": "/docs",
            "liveness": "/health",
            "readiness": "/ready",
            "capabilities": "/v1/capabilities",
            "agent_hints": "/v1/agent-hints",
        },
        "recommended_endpoints": ["GET /health", "GET /ready", "GET /v1/capabilities", "GET /v1/agent-hints", "POST /v1/crawl", "POST /v1/render"],
        "description": "Policy-driven universal crawler execution service for script-tool municipal crawling.",
    }


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

    @app.get(
        "/health",
        tags=["Health"],
        summary="Service liveness check",
        description="Lightweight process liveness endpoint used by Docker healthchecks. It does not inspect crawl saturation or start a browser.",
    )
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get(
        "/ready",
        response_model=ReadinessResponse,
        tags=["Health"],
        summary="Service readiness check",
        description="Reports active crawl load and configured capacity. Returns HTTP 503 when crawl work is saturated; this is backpressure, not a liveness failure.",
        responses={503: {"model": ReadinessResponse, "description": "Crawler is saturated but the process is alive."}},
    )
    async def ready() -> JSONResponse:
        payload = readiness_state.snapshot()
        return JSONResponse(status_code=503 if payload["saturated"] else 200, content=payload)

    @app.get(
        "/v1/capabilities",
        response_model=CapabilitiesResponse,
        tags=["Discovery"],
        summary="Describe crawler capabilities",
        description="Returns machine-readable strategies, classifications, output fields, defaults, limits, feature flags, and documentation URLs.",
        responses={500: {"model": CrawlerError, "description": "Unexpected service metadata error."}},
    )
    async def capabilities() -> dict[str, Any]:
        return capabilities_payload()

    @app.get(
        "/v1/agent-hints",
        response_model=AgentHintsResponse,
        tags=["Discovery"],
        summary="Provide crawler guidance for agents",
        description="Concise operational guidance and example policy snippets for AI agents and automation clients.",
    )
    async def agent_hints() -> dict[str, Any]:
        return agent_hints_payload()

    @app.get(
        "/.well-known/crawler-service",
        response_model=DiscoveryResponse,
        tags=["Discovery"],
        summary="Discover crawler service contracts",
        description="Stable discovery document pointing agents to OpenAPI, capabilities, hints, and supported endpoints.",
    )
    async def well_known_crawler_service() -> dict[str, Any]:
        return discovery_payload()

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
            result = await render_request(payload, settings=settings, worker=lambda job: renderer.render(job.url, job.options))
            logger.info("render_request_completed", extra={"url_count": len(result.get("results", []))})
            return result
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
                                    "policy": EXAMPLE_POLICIES["browser_fallback"],
                                },
                            },
                            "document_extraction": {
                                "summary": "Document extraction policy",
                                "value": {
                                    "urls": ["https://records.example.gov/agenda.pdf"],
                                    "policy": EXAMPLE_POLICIES["document_extraction"],
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
                    "policy": EXAMPLE_POLICIES["browser_fallback"],
                }
            ],
        )
    ) -> CondensedCrawlResponse:
        readiness_state.begin_request()
        try:
            # Note: renderer.render is now async with Camoufox
            result = await crawl_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                browser_worker=lambda job: asyncio.create_task(renderer.render(job.url, job.options)),
            )
            logger.info("crawl_request_completed", extra={"url_count": len(result.get("results", []))})

            # Transform to Firecrawl-style condensed output
            condensed_data = []
            for item in result.get("results", []):
                html = item.get("html")
                url = item.get("final_url") or item.get("normalized_url") or item.get("original_url")

                if html and url:
                    markdown, metadata_dict = extract_firecrawl_format(html, url)
                else:
                    markdown = item.get("text")
                    metadata_dict = {}

                # Build FirecrawlMetadata
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
        except ValueError as error:
            logger.warning("crawl_request_rejected", extra={"error": str(error)})
            raise HTTPException(status_code=400, detail={"code": str(error), "message": str(error), "details": {}}) from error
        finally:
            readiness_state.end_request()
else:
    app = None
