from __future__ import annotations

from typing import Any

from .api_models import (
    API_VERSION,
    POLICY_SCHEMA_VERSION,
    AgentHintsResponse,
    CapabilitiesResponse,
    CrawlClassification,
    CrawlerError,
    CrawlRequest,
    CrawlResponse,
    CrawlStrategy,
    DiscoveryResponse,
)
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


settings = CrawlerSettings.from_mapping()
renderer = BrowserRenderer()

STRATEGY_DESCRIPTIONS = {
    CrawlStrategy.HTTP_DECLARED_CRAWLER: "Direct HTTP using a transparent crawler user agent and caller-approved headers.",
    CrawlStrategy.HTTP_SITE_COMPAT: "Direct HTTP using browser-like compatibility headers before escalating to browser rendering.",
    CrawlStrategy.BROWSER_HEADLESS: "Chromium rendering in headless mode for JavaScript-dependent pages.",
    CrawlStrategy.BROWSER_HEADED_WARMED: "Headed Chromium with configured warmup URLs for challenge-prone municipal systems.",
    CrawlStrategy.DOCUMENT_EXTRACT: "Document/body extraction for PDFs and non-HTML responses when text evidence is needed.",
    CrawlStrategy.TRAFILATURA_EXTRACT: "Main-text extraction strategy for noisy HTML; currently exposed as policy surface and falls back to built-in text extraction.",
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
        "limits": {"timeout_seconds": 30, "max_retries": 2},
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
            "global_concurrency": settings.global_concurrency,
            "per_domain_concurrency": settings.per_domain_concurrency,
            "per_domain_delay_ms": settings.per_domain_delay_ms,
        },
        "features": {
            "headed_browser": True,
            "pdf_extraction": True,
            "direct_http": True,
            "trafilatura": False,
            "warmup": True,
            "domain_aware_concurrency": True,
        },
        "openapi_url": "/openapi.json",
        "swagger_ui_url": "/docs",
    }


def agent_hints_payload() -> dict[str, Any]:
    return {
        "service": "browser-crawler",
        "strategies": {strategy.value: description for strategy, description in STRATEGY_DESCRIPTIONS.items()},
        "guidance": [
            "Prefer direct transparent crawler HTTP for sites that allow it.",
            "Use browser strategies only when direct HTTP is blocked, incomplete, or requires rendering.",
            "Do not treat HTTP 200 as content success without checking classification and quality score.",
            "Preserve raw evidence and provenance in downstream artifacts.",
            "Do not mutate host policy automatically from a crawl result.",
            "Use attempt history to generate reviewable policy suggestions.",
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
            "capabilities": "/v1/capabilities",
            "agent_hints": "/v1/agent-hints",
        },
        "recommended_endpoints": ["GET /health", "GET /v1/capabilities", "GET /v1/agent-hints", "POST /v1/crawl", "POST /v1/render"],
        "description": "Policy-driven universal crawler execution service for script-tool municipal crawling.",
    }


if FastAPI is not None:
    app = FastAPI(
        title="Browser Crawler Execution Service",
        description=(
            "Localhost-only policy-driven crawler execution service for municipal script tools. "
            "Clients submit resolved crawl policies, inspect per-URL provenance, and use attempt history "
            "to make reviewable policy suggestions rather than mutating host policy automatically."
        ),
        version=API_VERSION,
        contact={"name": "dev-stack local services"},
        license_info={"name": "Local development service"},
        openapi_tags=[
            {"name": "Health", "description": "Liveness endpoints for local orchestration."},
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

    @app.get("/health", tags=["Health"], summary="Service liveness check")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

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
        try:
            return await render_request(payload, settings=settings, worker=lambda job: renderer.render(job.url, job.options))
        except ValueError as error:
            raise HTTPException(status_code=400, detail={"code": str(error), "message": str(error), "details": {}}) from error

    @app.post(
        "/v1/crawl",
        response_model=CrawlResponse,
        tags=["Crawler"],
        summary="Execute a policy-driven crawl",
        description=(
            "Executes the caller-provided strategy chain for one URL or a batch of URLs. "
            "The service records per-URL provenance, selected strategy, every attempted strategy, raw evidence, "
            "classification, quality score, and stable error details. Fallback stops at the first successful content result."
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
    ) -> dict[str, Any]:
        try:
            return await crawl_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                browser_worker=lambda job: renderer.render(job.url, job.options),
            )
        except ValueError as error:
            raise HTTPException(status_code=400, detail={"code": str(error), "message": str(error), "details": {}}) from error
else:
    app = None
