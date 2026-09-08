"""``/v1/render`` URL→handler mapping.

Pure router: imports :func:`app.api.render_endpoint.render` and binds it
to the ``/v1/render`` POST path. The handler implementation lives in the
endpoint module so the router stays trivial and the handler is testable
without FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.api.render_endpoint import render as render_handler
from app.api_models import CrawlerError, RenderRequest, UnifiedCrawlResponse

router = APIRouter(tags=["Crawler"])


@router.post(
    "/v1/render",
    summary="Render pages with the browser worker",
    description="Browser-render endpoint. Prefer POST /v1/crawl for policy-driven universal crawling.",
    responses={400: {"model": CrawlerError, "description": "Invalid render request."}},
    response_model=UnifiedCrawlResponse,
)
async def render(
    payload: RenderRequest,
    settings=Depends(get_settings),  # noqa: B008
) -> UnifiedCrawlResponse:
    return await render_handler(payload, settings=settings)


__all__ = ["router"]
