"""``/v1/search`` URL→handler mapping."""

from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.dependencies import get_settings
from app.api_models import (
    SearchRequest,
)
from app.constants import ROUTER_MODULE_DOC
from app.models.error_response import ErrorResponse
from app.models.search_response import SearchResponse
from app.search.endpoint import search as search_handler

__doc__ = f"""``/v1/search`` URL→handler mapping.

{ROUTER_MODULE_DOC.format(handler="app.search.endpoint.search", path="/v1/search")}
"""

router = APIRouter(tags=["Search"])


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    summary="Web search for public URLs from a query",
    description=(
        "Run a web search query through the configured provider and return SERP-style "
        "results (title, url, description). The service does NOT crawl result URLs; "
        "callers pipe them into POST /v1/crawl. The active provider is operator-configured "
        "via CRAWLUTRON_SEARCH_PROVIDER and the matching *_API_KEY; GET "
        "/v1/capabilities reports which provider is active. Results are normalized to "
        "public HTTP(S) URLs; loopback/private targets are dropped."
    ),
    responses={
        200: {"model": SearchResponse, "description": "Search results (JSON)."},
        422: {"model": ErrorResponse, "description": "Invalid search request."},
        502: {"model": ErrorResponse, "description": "The search provider returned an error."},
        503: {"model": ErrorResponse, "description": "The selected search provider is not configured."},
    },
)
async def search(
    payload: SearchRequest,
    settings=Depends(get_settings),
) -> SearchResponse:
    return await search_handler(payload, settings=settings)


__all__ = ["router"]
