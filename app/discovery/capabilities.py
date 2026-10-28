"""Capabilities discovery endpoint."""

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.config.settings import CrawlerSettings
from app.models.auth_capabilities_record import AuthCapabilitiesRecord
from app.models.capabilities_response import CapabilitiesPayload, CapabilitiesResponse
from app.models.crawl_extraction_method import CrawlExtractionMethod
from app.models.crawl_strategy import CrawlStrategy
from app.models.crawl_timing_record import CrawlTimingRecord
from app.models.mcp_capabilities_record import McpCapabilitiesRecord
from app.models.search_capabilities_record import SearchCapabilitiesRecord
from app.models.service_identity import SERVICE_IDENTITY
from app.search import active_search_provider_name, configured_search_providers
from app.vocabulary.limits import (
    DEFAULT_CHALLENGE_WAIT_SECONDS,
    DEFAULT_MAX_DISCOVERED_LINKS,
    DEFAULT_TOTAL_URL_BUDGET_SECONDS,
    LARGE_CRAWL_GUIDANCE,
)
from app.vocabulary.taxonomy import CrawlClassification

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


def capabilities_payload(settings: CrawlerSettings) -> CapabilitiesPayload:
    """The whole discovery payload, one named section per concern."""
    return {
        "service": SERVICE_IDENTITY["service"],
        "version": SERVICE_IDENTITY["version"],
        "policy_schema_version": SERVICE_IDENTITY["policy_schema_version"],
        "supported_strategies": [strategy.value for strategy in CrawlStrategy],
        "supported_extractions": [method.value for method in CrawlExtractionMethod],
        "supported_classifications": [c.value for c in CrawlClassification],
        "output_fields": OUTPUT_FIELDS,
        "default_limits": _default_limit_fields(settings),
        "features": _feature_flag_fields(),
        "auth": _auth_fields(),
        "search": _search_fields(settings),
        "mcp": _mcp_fields(),
        "crawl_timing": _crawl_timing_fields(),
        "openapi_url": "/openapi.json",
        "swagger_ui_url": "/docs",
    }


def _default_limit_fields(settings: CrawlerSettings) -> dict[str, int | float]:
    """The limits a caller must plan around, from settings and the vocabulary."""
    return {
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
    }


def _feature_flag_fields() -> dict[str, bool]:
    """Every capability the service advertises, refusals included, by name."""
    return {
        "headed_browser": True,
        "pdf_extraction": True,
        "direct_http": True,
        "trafilatura": True,
        "discovered_links": True,
        "frame_discovery": True,
        "form_discovery": True,
        "conditional_fetch": True,
        # The stored crawl record's diagnostic spine, read over existing
        # data: attempts and classification evidence without page bodies.
        "per_request_trace": True,
        # Candidate host rules proposed from recorded evidence, for a person
        # to review. Nothing here applies a rule.
        "policy_suggestions": True,
        # The surface that uses `conditional_fetch` on the caller's behalf:
        # registered URLs the sweep rechecks, and announces when they change.
        "watches": True,
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
        "credential_injection": False,
        # interstitial is a challenge, so captcha_solving: false states it already;
        # naming the capability is the advertisement, not the warning.
        "robots_diagnostics": True,
        "sitemap_discovery": True,
        "feed_discovery": True,
        "adaptive_retry": True,
        "runtime_diagnostics": True,
        "domain_aware_concurrency": True,
        "readiness_endpoint": True,
        "event_loop_thread_offload": True,
    }


def _auth_fields() -> AuthCapabilitiesRecord:
    """How a caller identifies, and who can mint keys."""
    return {
        "caller_credential": "X-API-Key or Authorization: Bearer",
        "anonymous_caller": "shared bounded identity (default quotas)",
        "key_management": "POST /v1/keys, operator-gated (X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN)",
    }


def _search_fields(settings: CrawlerSettings) -> SearchCapabilitiesRecord:
    """The search front-end's provider picture and its selection rule."""
    return {
        "active_provider": active_search_provider_name(settings),
        "configured_providers": configured_search_providers(settings),
        "endpoint": "/v1/search",
        "provider_selection": "CRAWLUTRON_SEARCH_PROVIDER (auto "
        "picks the first keyed provider; DuckDuckGo is the keyless fallback)",
    }


def _mcp_fields() -> McpCapabilitiesRecord:
    """The MCP surface's endpoint and the protocol versions it speaks."""
    return {
        "endpoint": "/mcp",
        "protocol_versions": ["2026-07-28"],
    }


def _crawl_timing_fields() -> CrawlTimingRecord:
    """Per-URL budget facts and the large-crawl guidance text."""
    return {
        "per_url_budget_seconds": DEFAULT_TOTAL_URL_BUDGET_SECONDS,
        "sync_has_server_time_ceiling": False,
        "large_crawl_guidance": LARGE_CRAWL_GUIDANCE,
    }


def create_capabilities_router() -> APIRouter:
    """Create capabilities router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/v1/capabilities", response_model=CapabilitiesResponse)
    async def capabilities(settings: CrawlerSettings = Depends(get_settings)) -> CapabilitiesPayload:
        """Describe crawler capabilities."""
        return capabilities_payload(settings)

    return router
