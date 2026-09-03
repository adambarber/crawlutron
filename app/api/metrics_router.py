"""``/metrics`` Prometheus-format endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Response

from app.metrics import get_metrics

router = APIRouter(tags=["Health"])


@router.get(
    "/metrics",
    summary="Prometheus metrics endpoint (#16)",
)
async def metrics_endpoint() -> Response:
    """Return the crawler metrics in Prometheus exposition format."""
    return Response(get_metrics().as_prometheus(), media_type="text/plain; version=0.0.4")


__all__ = ["router"]
