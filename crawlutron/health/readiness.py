"""Health and readiness endpoints (self-managed; no external config)."""

import threading
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from crawlutron.config.env import get_env_int
from crawlutron.models.health_response import HealthResponse
from crawlutron.models.readiness_response import ReadinessResponse
from crawlutron.runtime.metrics import get_metrics

# The service reports saturated (HTTP 503) when its own guard rides its ceiling, instead
# of a removed external concurrency knob.
_MEMORY_SATURATION = 0.92
_CPU_SATURATION = 0.90


@dataclass(frozen=True)
class ReadinessSnapshot:
    """Typed view of the readiness state at one instant.

    Declaring the four fields pins the shape at the source instead of a widened
    ``dict[str, int | bool | str]`` that forced per-key asserts at construction.
    """

    status: str
    active_requests: int
    max_active_requests: int
    saturated: bool


def _read_max_active_cap() -> int | None:
    """The configured admission cap from the env knob, or None when disabled.

    Unset and 0 both mean "no cap"; only a positive value is a real bound.
    """
    cap = get_env_int("CRAWLUTRON_MAX_ACTIVE_REQUESTS", 0, lo=0)
    return cap if cap > 0 else None


class ReadinessState:
    """Thread-safe readiness tracker: saturation from the resource guard,
    active-request count as visibility plus an optional fixed cap.
    """

    def __init__(self, max_active_requests: int | None = None) -> None:
        self._active_requests = 0
        self._lock = threading.Lock()
        self._utilization_sampler: Callable[[], tuple[float, float]] | None = None
        # The cap: None means disabled (optional-presence), a positive value the
        # bound; an injected value bypasses the env knob in tests.
        configured = _read_max_active_cap() if max_active_requests is None else max(0, max_active_requests)
        self._max_active_requests: int | None = configured if configured else None

    def set_utilization_sampler(self, sampler: Callable[[], tuple[float, float]] | None) -> None:
        """Install a live ``(memory, cpu)`` utilization source.

        The lifespan wires the engine's ResourceGate here so /ready reflects current
        pressure without a recursive crawl refreshing the gauges; ``None`` restores them.
        """
        with self._lock:
            self._utilization_sampler = sampler

    def begin_request(self) -> bool:
        """Admit one request: returns True and holds a slot, or False when the
        fixed cap is reached -- the caller refuses, never taking the slot.
        """
        with self._lock:
            cap = self._max_active_requests
            if cap is not None and self._active_requests >= cap:
                return False
            self._active_requests += 1
            return True

    def end_request(self) -> None:
        with self._lock:
            self._active_requests = max(0, self._active_requests - 1)

    def snapshot(self) -> ReadinessSnapshot:
        with self._lock:
            active_requests = self._active_requests
            sampler = self._utilization_sampler
        if sampler is not None:
            try:
                mem_util, cpu_util = (float(v) for v in sampler())
            except Exception:
                sampler = None
        if sampler is None:
            metrics = get_metrics()
            mem_util = float(getattr(metrics, "resource_memory_utilization", 0.0) or 0.0)
            cpu_util = float(getattr(metrics, "resource_cpu_utilization", 0.0) or 0.0)
        saturated = mem_util >= _MEMORY_SATURATION or cpu_util >= _CPU_SATURATION
        return ReadinessSnapshot(
            status="saturated" if saturated else "ready",
            active_requests=active_requests,
            # The real bound: the configured cap, or 0 when none is set (off).
            max_active_requests=self._max_active_requests or 0,
            saturated=saturated,
        )


# A module-level singleton so routers outside this module call begin_request /
# end_request without depending on create_health_router being called first.
READINESS_STATE = ReadinessState()


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
