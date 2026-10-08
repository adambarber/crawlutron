"""``/v1/render`` URL→handler mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.api.render_endpoint import render as render_handler
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
    settings=Depends(get_settings),
) -> UnifiedCrawlResponse:
    return await render_handler(payload, settings=settings)


__all__ = ["router"]
