"""Health and readiness endpoints (self-managed; no external config).

State lives in ``crawlutron.runtime.readiness_state``; patch paths keep working.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crawlutron.models.health_response import HealthResponse
from crawlutron.models.readiness_response import ReadinessResponse
from crawlutron.runtime.readiness_state import (
    READINESS_STATE,
    ReadinessSnapshot,
    ReadinessState,
)

__all__ = ["READINESS_STATE", "ReadinessSnapshot", "ReadinessState", "create_health_router"]


def create_health_router() -> "tuple[APIRouter, ReadinessState]":
    """Create health and readiness router (self-managed; no external config)."""
    router = APIRouter(tags=["Health"])
    readiness_state = READINESS_STATE

    @router.get(
        "/health",
        response_model=HealthResponse,
        summary="Service liveness check",
        description=(
            "Lightweight process liveness endpoint used by Docker healthchecks. "
            "It does not inspect crawl saturation or start a browser."
        ),
    )
    async def health() -> HealthResponse:
        """Liveness check - always returns ok."""
        return HealthResponse(status="ok")

    @router.get(
        "/ready",
        response_model=ReadinessResponse,
        summary="Service readiness check",
        description=(
            "Reports resource saturation using the service's own self-managed guard "
            "(memory/CPU utilization at the gate target). Returns 503 when saturated so "
            "callers can apply backpressure. Unrelated to a fixed request cap."
        ),
        responses={
            503: {
                "description": "Service saturated - resource guard at capacity (memory or CPU at target).",
                "content": {
                    "application/json": {"schema": {"$ref": "#/components/schemas/ReadinessResponse"}}
                },
            }
        },
    )
    async def ready() -> JSONResponse:
        """Readiness check - returns HTTP 503 when the self-managed guard is saturated."""
        snap = readiness_state.snapshot()
        return JSONResponse(
            status_code=503 if snap.saturated else 200,
            content=ReadinessResponse(
                status=snap.status,
                active_requests=snap.active_requests,
                max_active_requests=snap.max_active_requests,
                saturated=snap.saturated,
            ).model_dump(),
        )

    return router, readiness_state
