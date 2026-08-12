"""Well-known discovery endpoint."""

from typing import Any

from fastapi import APIRouter

from app.api_models import DiscoveryResponse
from app.settings import CrawlerSettings


API_VERSION = "1.0.0"
POLICY_SCHEMA_VERSION = "1.0"


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


def create_well_known_router(settings: CrawlerSettings) -> APIRouter:
    """Create well-known discovery router."""
    router = APIRouter(tags=["Discovery"])

    @router.get("/.well-known/crawler-service", response_model=DiscoveryResponse)
    async def well_known_crawler_service() -> dict[str, Any]:
        """Discover crawler service contracts."""
        return discovery_payload()

    return router
