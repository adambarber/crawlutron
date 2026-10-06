"""Well-known discovery endpoint."""

from typing import Any

from fastapi import APIRouter

from app.models.discovery_response import DiscoveryResponse
from app.models.service_identity import SERVICE_IDENTITY


def discovery_payload() -> dict[str, Any]:
    return {
        **SERVICE_IDENTITY,
        "links": {
            "openapi": "/openapi.json",
            "swagger_ui": "/docs",
            "liveness": "/health",
            "readiness": "/ready",
            "capabilities": "/v1/capabilities",
            "agent_hints": "/v1/agent-hints",
            "search": "/v1/search",
            "map": "/v1/map",
            "jobs": "/v1/jobs",
            "result_retrieval": "/v1/crawl/{request_id}",
            "mcp": "/mcp",
        },
        "recommended_endpoints": [
            "GET /health",
            "GET /ready",
            "GET /v1/capabilities",
            "GET /v1/agent-hints",
            "POST /v1/crawl",
            "POST /v1/map",
            "POST /v1/render",
            "POST /v1/search",
            "POST /v1/jobs",
            "GET /v1/jobs",
            "GET /v1/jobs/{job_id}",
            "POST /v1/jobs/{job_id}/control",
            "DELETE /v1/jobs/{job_id}",
            "GET /v1/crawl/{request_id}",
            "POST /mcp",
        ],
        "description": "Policy-driven universal crawler execution service for script-tool municipal crawling.",  # noqa: E501
    }


def create_well_known_router() -> APIRouter:
    """Create well-known discovery router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/.well-known/crawler-service", response_model=DiscoveryResponse)
    async def well_known_crawler_service() -> dict[str, Any]:
        """Discover crawler service contracts."""
        return discovery_payload()

    return router
