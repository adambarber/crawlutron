"""``/metrics`` Prometheus-format endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request, Response

from app.api.caller_auth import is_operator
from app.runtime.metrics import get_metrics

router = APIRouter(tags=["Health"])


@router.get(
    "/metrics",
    summary="Prometheus metrics endpoint",
)
async def metrics_endpoint(request: Request) -> Response:
    """Return metrics in Prometheus exposition format.

    The base exposition stays loopback-assessable; the per-caller detail families
    require the operator token (CRAWLUTRON_KEY_ADMIN_TOKEN) because they name keys.
    """
    return Response(
        get_metrics().as_prometheus(caller_detail=is_operator(request)),
        media_type="text/plain; version=0.0.4",
    )


__all__ = ["router"]
