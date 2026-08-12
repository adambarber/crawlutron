"""Health and readiness endpoints."""

import threading
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.api_models import ReadinessResponse
from app.settings import CrawlerSettings


class ReadinessState:
    """Thread-safe readiness state tracker."""

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


def create_health_router(settings: CrawlerSettings) -> APIRouter:
    """Create health and readiness router."""
    router = APIRouter(tags=["Health"])
    readiness_state = ReadinessState(max_active_requests=settings.global_concurrency)

    class HealthResponse(BaseModel):
        status: str = Field(description="Always 'ok' for liveness check.")

    @router.get("/health", response_model=HealthResponse)
    async def health() -> HealthResponse:
        """Liveness check - always returns ok."""
        return HealthResponse(status="ok")

    @router.get("/ready", response_model=ReadinessResponse)
    async def ready() -> ReadinessResponse:
        """Readiness check - returns saturated when at capacity."""
        snap = readiness_state.snapshot()
        return ReadinessResponse(
            status=snap["status"],
            active_requests=snap["active_requests"],
            max_active_requests=snap["max_active_requests"],
            saturated=snap["saturated"],
        )

    return router, readiness_state
