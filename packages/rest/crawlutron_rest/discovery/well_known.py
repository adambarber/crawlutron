"""Well-known discovery endpoint (the machine door the 404 details advertise)."""

from fastapi import APIRouter, Depends

from crawlutron.config.settings import CrawlerSettings
from crawlutron.discovery.links import OPENAPI_DOCS_LINK
from crawlutron.models.discovery_response import DiscoveryPayload, DiscoveryResponse
from crawlutron.models.service_identity import SERVICE_IDENTITY
from crawlutron_rest.discovery.recommended import recommended_endpoints


def discovery_payload(settings: CrawlerSettings) -> DiscoveryPayload:
    return {
        **SERVICE_IDENTITY,
        "links": {
            "openapi": "/openapi.json",
            "swagger_ui": OPENAPI_DOCS_LINK,
            "liveness": "/health",
            "readiness": "/ready",
            "capabilities": "/v1/capabilities",
            "agent_hints": "/v1/agent-hints",
            "admit": "/v1/admit",
            "search": "/v1/search",
            "map": "/v1/map",
            "jobs": "/v1/jobs",
            "watches": "/v1/watches",
            "keys": "/v1/keys",
            "result_retrieval": "/v1/crawl/{request_id}",
            "trace": "/v1/crawl/{request_id}/trace",
            "policy_suggestions": "/v1/policy-suggestions",
            "mcp": "/mcp",
        },
        "recommended_endpoints": recommended_endpoints(),
        "description": "Policy-driven universal crawler execution service.",
        "auth": {
            "caller_keys": "Present X-API-Key (or Authorization: Bearer) on crawl/job/result requests; "
            "jobs and results created under a key are readable only under it. Anonymous requests crawl as a "
            "shared bounded identity.",
            "key_issuance": "POST /v1/keys mints and revokes caller keys but accepts only the operator token "
            "(X-API-Key: CRAWLUTRON_KEY_ADMIN_TOKEN); with no token configured the keys surface is sealed.",
        },
        "is_operator_sealed": not bool(settings.key_admin_token),
    }


def create_well_known_router() -> APIRouter:
    """Create well-known discovery router."""
    from crawlutron_rest.api.dependencies import get_settings as api_get_settings

    router = APIRouter(tags=["Discovery"])

    @router.get("/.well-known/crawler-service", response_model=DiscoveryResponse)
    async def well_known_crawler_service(
        settings: CrawlerSettings = Depends(api_get_settings),
    ) -> DiscoveryPayload:
        """Discover crawler service contracts."""
        return discovery_payload(settings)

    return router
