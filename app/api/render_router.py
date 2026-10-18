"""``/v1/render`` URL→handler mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from app.api.caller_auth import active_sync_for, check_concurrency_quota, check_page_quota
from app.api.dependencies import CallerDep, get_settings
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
    caller: CallerDep,
    request: Request,
    settings=Depends(get_settings),
) -> UnifiedCrawlResponse:
    # #205: enforce the caller concurrency+page quota before the render runs.
    requested = len(payload.urls) if payload.urls is not None else 1
    check_page_quota(caller, requested)
    check_concurrency_quota(caller, active_sync_for(caller, request))
    return await render_handler(payload, settings=settings)


__all__ = ["router"]
