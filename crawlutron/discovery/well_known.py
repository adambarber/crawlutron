"""Well-known discovery endpoint."""

from fastapi import APIRouter, Depends

from crawlutron.api.dependencies import get_settings
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.discovery_response import DiscoveryPayload, DiscoveryResponse
from crawlutron.models.service_identity import SERVICE_IDENTITY


def discovery_payload(settings: CrawlerSettings) -> DiscoveryPayload:
    return {
        **SERVICE_IDENTITY,
        "links": {
            "openapi": "/openapi.json",
            "swagger_ui": "/docs",
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
        "recommended_endpoints": [
            "GET /health",
            "GET /ready",
            "GET /v1/capabilities",
            "GET /v1/agent-hints",
            "POST /v1/admit",
            "POST /v1/crawl",
            "POST /v1/map",
            "POST /v1/render",
            "POST /v1/search",
            "POST /v1/jobs",
            "GET /v1/jobs",
            "GET /v1/jobs/{job_id}",
            "POST /v1/jobs/{job_id}/control",
            "DELETE /v1/jobs/{job_id}",
            "POST /v1/keys",
            "GET /v1/keys",
            "DELETE /v1/keys/{key_id}",
            "POST /v1/watches",
            "GET /v1/watches",
            "GET /v1/watches/{watch_id}",
            "DELETE /v1/watches/{watch_id}",
            "GET /v1/crawl/{request_id}",
            "GET /v1/crawl/{request_id}/trace",
            "GET /v1/policy-suggestions",
            "POST /mcp",
        ],
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
    router = APIRouter(tags=["Discovery"])

    @router.get("/.well-known/crawler-service", response_model=DiscoveryResponse)
    async def well_known_crawler_service(
        settings: CrawlerSettings = Depends(get_settings),
    ) -> DiscoveryPayload:
        """Discover crawler service contracts."""
        return discovery_payload(settings)

    return router
