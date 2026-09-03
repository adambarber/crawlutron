"""``/v1/render`` URL→handler mapping.

Pure router: imports :func:`app.api.render_endpoint.render` and binds it
to the ``/v1/render`` POST path. The handler implementation lives in the
endpoint module so the router stays trivial and the handler is testable
without FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.render_endpoint import render
from app.api_models import CrawlerError, UnifiedCrawlResponse

router = APIRouter(tags=["Crawler"])

router.add_api_route(
    "/v1/render",
    render,
    methods=["POST"],
    summary="Render pages with the browser worker",
    description="Backward-compatible browser-render endpoint. Prefer POST /v1/crawl for policy-driven universal crawling.",
    responses={400: {"model": CrawlerError, "description": "Invalid render request."}},
    response_model=UnifiedCrawlResponse,
)


__all__ = ["router"]
