"""Capabilities discovery endpoint."""

from typing import Any

from fastapi import APIRouter

from app.api_models import (
    CapabilitiesResponse,
    CrawlClassification,
    CrawlStrategy,
)
from app.settings import CrawlerSettings


from app.api_models import API_VERSION, POLICY_SCHEMA_VERSION

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


def capabilities_payload(settings: CrawlerSettings) -> dict[str, Any]:
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


def create_capabilities_router(settings: CrawlerSettings) -> APIRouter:
    """Create capabilities router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/capabilities", response_model=CapabilitiesResponse)
    async def capabilities() -> dict[str, Any]:
        """Describe crawler capabilities."""
        return capabilities_payload(settings)

    return router
