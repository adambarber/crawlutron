from __future__ import annotations

from fastapi import APIRouter

from app.api.render_endpoint import render as render_handler
from app.api.router_surface import CallerDep, TrackingDep, admit, settings_dep
from app.models.error_response import ErrorResponse
from app.models.render_request import RenderRequest
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.vocabulary.identity import ROUTER_MODULE_DOC

__doc__ = f"""``/v1/render`` URL→handler mapping.

{ROUTER_MODULE_DOC.format(handler="app.api.render_endpoint.render", path="/v1/render")}
"""

router = APIRouter(tags=["Crawler"])


@router.post(
    "/v1/render",
    summary="Render pages with the browser worker",
    description="Browser-render endpoint. Prefer POST /v1/crawl for policy-driven universal crawling.",
    responses={400: {"model": ErrorResponse, "description": "Invalid render request."}},
    response_model=UnifiedCrawlResponse,
)
async def render(
    payload: RenderRequest,
    caller: CallerDep,
    tracking: TrackingDep,
    settings=settings_dep,
) -> UnifiedCrawlResponse:
    # #205/#320: admit the render through the one helper -- page quota, quotas
    # against the shared register, and a released slot for the request's life.
    requested = len(payload.urls) if payload.urls is not None else 1
    async with admit(caller, requested, tracking=tracking):
        return await render_handler(payload, settings=settings)


__all__ = ["router"]
