"""The identity-guarded door plane: map, render and search share one router.

Each door wraps the caller's admission in ``request_gate`` (readiness OUTER) then
admits through the single register (INNER) -- the one order every door shares.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from fastapi import APIRouter

from app.api._depends_singletons import settings_dep, shutdown_event_dep
from app.api.caller_auth import admit
from app.api.dependencies import CallerDep, TrackingDep
from app.api.main_helpers import request_gate
from app.api.map_endpoint import map_site
from app.api.render_endpoint import _render_rejection
from app.api.render_endpoint import render as render_handler
from app.models import map_request, render_request, search_request
from app.models.error_response import ErrorResponse
from app.models.search_response import SearchResponse
from app.models.site_map_response import SiteMapResponse
from app.models.unified_crawl_response import UnifiedCrawlResponse
from app.search.endpoint import search as search_handler

if TYPE_CHECKING:
    from app.models.site_map_response import SiteMapPayload

logger = logging.getLogger(__name__)

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
    payload: map_request.MapRequest,
    caller: CallerDep,
    tracking: TrackingDep,
    settings=settings_dep,
    shutdown_event=shutdown_event_dep,
) -> SiteMapPayload:
    # #205/#320/#328: request_gate OUTER (the readiness count includes quota-
    # rejected requests), admit INNER -- the one order every door shares.
    with request_gate("map_request_rejected", logger=logger):
        async with admit(
            caller,
            payload.policy.limits.max_pages,
            tracking=tracking,
            payload=payload,
        ):
            return await map_site(payload, settings=settings, shutdown_event=shutdown_event)


@router.post(
    "/v1/render",
    summary="Render pages with the browser worker",
    description="Browser-render endpoint. Prefer POST /v1/crawl for policy-driven universal crawling.",
    responses={400: {"model": ErrorResponse, "description": "Invalid render request."}},
    response_model=UnifiedCrawlResponse,
)
async def render(
    payload: render_request.RenderRequest,
    caller: CallerDep,
    tracking: TrackingDep,
    settings=settings_dep,
) -> UnifiedCrawlResponse:
    requested = len(payload.urls) if payload.urls is not None else 1
    with request_gate("render_request_rejected", logger=logger, reject=_render_rejection):
        async with admit(caller, requested, tracking=tracking):
            return await render_handler(payload, settings=settings)


@router.post(
    "/v1/search",
    tags=["Search"],
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
    payload: search_request.SearchRequest,
    caller: CallerDep,
    tracking: TrackingDep,
    settings=settings_dep,
) -> SearchResponse:
    with request_gate("search_request_rejected", logger=logger):
        async with admit(caller, 0, tracking=tracking):
            return await search_handler(payload, settings=settings, caller=caller)


__all__ = ["router"]
