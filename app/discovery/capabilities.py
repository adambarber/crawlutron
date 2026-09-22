"""Capabilities discovery endpoint."""

from typing import Any

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.api_models import (
    POLICY_SCHEMA_VERSION,
    CapabilitiesResponse,
    CrawlClassification,
    CrawlExtractionMethod,
    CrawlStrategy,
)
from app.constants import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    LARGE_CRAWL_GUIDANCE,
    SERVICE_VERSION,
)
from app.search import active_search_provider_name, configured_search_providers
from app.settings import CrawlerSettings

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
        "version": SERVICE_VERSION,
        "policy_schema_version": POLICY_SCHEMA_VERSION,
        "supported_strategies": [strategy.value for strategy in CrawlStrategy],
        "supported_extractions": [method.value for method in CrawlExtractionMethod],
        "supported_classifications": [classification.value for classification in CrawlClassification],
        "output_fields": OUTPUT_FIELDS,
        "default_limits": {
            "batch_size": settings.max_batch_size,
            "max_batch_size": settings.max_batch_size,
            "timeout_seconds": settings.default_timeout_seconds,
            "max_retries": settings.default_max_retries,
            "challenge_wait_seconds": DEFAULT_CHALLENGE_WAIT_SECONDS,
            "max_discovered_links": DEFAULT_MAX_DISCOVERED_LINKS,
            "total_url_budget_seconds": DEFAULT_TOTAL_URL_BUDGET_SECONDS,
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
            "conditional_fetch": True,
            "recursive_frontier": True,
            "warmup": True,
            "request_profiles": True,
            "browser_profiles": True,
            "web_search": True,
            "mcp": True,
            "async_mode": True,
            "captcha_solving": False,
            "proxy_rotation": False,
            "identity_rotation": False,
            "credential_bypass": False,
            # The service passively waits out challenges (challenge_wait_seconds);
            # it does NOT solve or bypass them, so do not advertise bypass.
            "cloudflare_bypass": False,
            "robots_diagnostics": True,
            "sitemap_discovery": True,
            "feed_discovery": True,
            "adaptive_retry": True,
            "runtime_diagnostics": True,
            "domain_aware_concurrency": True,
            "readiness_endpoint": True,
            "event_loop_thread_offload": True,
        },
        "search": {
            "active_provider": active_search_provider_name(settings),
            "configured_providers": configured_search_providers(settings),
            "endpoint": "/v1/search",
            "provider_selection": "BROWSER_CRAWLER_SEARCH_PROVIDER (auto picks the first keyed provider; DuckDuckGo is the keyless fallback)",
        },
        "mcp": {
            "endpoint": "/mcp",
            "protocol_versions": ["2026-07-28"],
        },
        "crawl_timing": {
            "per_url_budget_seconds": DEFAULT_TOTAL_URL_BUDGET_SECONDS,
            "sync_has_server_time_ceiling": False,
            "large_crawl_guidance": LARGE_CRAWL_GUIDANCE,
        },
        "openapi_url": "/openapi.json",
        "swagger_ui_url": "/docs",
    }


def create_capabilities_router() -> APIRouter:
    """Create capabilities router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/capabilities", response_model=CapabilitiesResponse)
    async def capabilities(settings: CrawlerSettings = Depends(get_settings)) -> dict[str, Any]:
        """Describe crawler capabilities."""
        return capabilities_payload(settings)

    return router
