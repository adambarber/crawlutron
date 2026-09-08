"""``/v1/search`` URL→handler mapping.

Pure router: imports :func:`app.search.endpoint.search` and binds it to the
``/v1/search`` POST path. The handler implementation lives in the endpoint
module so the router stays trivial and the handler is testable without
FastAPI machinery.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.api_models import CrawlerError, SearchRequest, SearchResponse
from app.search.endpoint import search as search_handler

router = APIRouter(tags=["Search"])


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    summary="Web search for public URLs from a query",
    description=(
        "Run a web search query through the configured provider and return SERP-style "
        "results (title, url, description). The service does NOT crawl result URLs; "
        "callers pipe them into POST /v1/crawl. The active provider is operator-configured "
        "via BROWSER_CRAWLER_SEARCH_PROVIDER and the matching *_API_KEY; GET "
        "/v1/capabilities reports which provider is active. Results are normalized to "
        "public HTTP(S) URLs; loopback/private targets are dropped."
    ),
    responses={
        200: {"model": SearchResponse, "description": "Search results (JSON)."},
        422: {"model": CrawlerError, "description": "Invalid search request."},
        502: {"model": CrawlerError, "description": "The search provider returned an error."},
        503: {"model": CrawlerError, "description": "The selected search provider is not configured."},
    },
)
async def search(
    payload: SearchRequest,
    settings=Depends(get_settings),  # noqa: B008
) -> SearchResponse:
    return await search_handler(payload, settings=settings)


__all__ = ["router"]
