"""``/v1/map`` URL→handler mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings, get_shutdown_event
from app.api.map_endpoint import map_site
from app.models.error_response import ErrorResponse
from app.models.map_request import MapRequest
from app.models.site_map_response import SiteMapPayload, SiteMapResponse

router = APIRouter(tags=["Crawler"])


@router.post(
    "/v1/map",
    summary="Discover the URL frontier for a root URL",
    description=(
        "Map a root URL to the URLs reachable from it: in-page discovered links plus the "
        "URLs advertised via robots.txt sitemaps and feeds. Discovery is always enabled; the "
        "caller controls the fetch policy (strategies, limits, scope_regex). The service returns "
        "the de-duplicated frontier but does not recursively enqueue it; callers own recursion, "
        "dedupe, persistence, and host-policy decisions."
    ),
    responses={400: {"model": ErrorResponse, "description": "Invalid map request."}},
    response_model=SiteMapResponse,
)
async def map(
    payload: MapRequest,
    settings=Depends(get_settings),
    shutdown_event=Depends(get_shutdown_event),
) -> SiteMapPayload:
    return await map_site(payload, settings=settings, shutdown_event=shutdown_event)


__all__ = ["router"]
