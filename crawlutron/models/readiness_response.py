from __future__ import annotations

from pydantic import BaseModel, Field


class ReadinessResponse(BaseModel):
    status: str = Field(
        description="Capacity state: ready below the self-managed resource ceiling, saturated when memory or "
        "CPU utilization rides at the gate target."
    )
    active_requests: int = Field(
        description="Currently active /v1/crawl and /v1/render requests tracked by the service."
    )
    max_active_requests: int = Field(
        description="0 = unbounded; saturation is resource-driven (memory/CPU utilization at the "
        "self-managed "
        "gate target), not a fixed request cap."
    )
    saturated: bool = Field(
        description="True when the self-managed resource guard is at capacity; callers should treat HTTP 503 "
        "as backpressure, not process death."
    )
