"""``/metrics`` Prometheus-format endpoint."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, Request, Response

from crawlutron.runtime.metrics import get_metrics
from crawlutron_rest.api.route_layer import operator_guard

router = APIRouter(tags=["Health"], dependencies=[Depends(operator_guard)])


@router.get(
    "/metrics",
    summary="Prometheus metrics endpoint",
)
async def metrics_endpoint(request: Request) -> Response:
    """Return metrics in Prometheus exposition format.

    The base exposition stays loopback-assessable; the per-caller detail families
    require the operator token (CRAWLUTRON_KEY_ADMIN_TOKEN) because they name keys.
    """
    metrics = get_metrics()
    # ``as_prometheus`` reads the shared coordination counters -- a dozen SQLite
    # reads per scrape. Run it off the loop so a scrape cannot stall on the store.
    return Response(
        await asyncio.to_thread(metrics.as_prometheus, caller_detail=getattr(request.state, "caller", False)),
        media_type="text/plain; version=0.0.4",
    )


__all__ = ["router"]
