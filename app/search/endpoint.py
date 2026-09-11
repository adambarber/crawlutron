"""``/v1/search`` endpoint handler."""

from __future__ import annotations

import logging
import time

from app.api.exceptions import http_error
from app.api_models import SearchRequest, SearchResponse, SearchResultItem
from app.health.readiness import READINESS_STATE as readiness_state
from app.metrics import get_metrics
from app.search.providers import (
    SearchProviderError,
    SearchProviderUnavailable,
    get_search_provider,
)

logger = logging.getLogger(__name__)


async def search(
    payload: SearchRequest,
    *,
    settings,
) -> SearchResponse:
    """Run a web search against the configured provider and shape the results."""
    readiness_state.begin_request()
    try:
        try:
            provider = get_search_provider(settings)
        except SearchProviderUnavailable as error:
            logger.warning("search_request_unavailable", extra={"error": str(error)})
            raise http_error(503, "search_provider_unavailable", str(error)) from error
        limit = max(1, min(payload.limit or settings.search_limit_default, settings.search_limit_max))
        started = time.monotonic()
        try:
            results = await provider.search(payload.query, limit, float(settings.search_timeout_seconds))
        except SearchProviderError as error:
            get_metrics().observe_search(provider=provider.name, errors=1)
            logger.warning(
                "search_request_provider_error",
                extra={"provider": provider.name, "error": str(error)},
            )
            raise http_error(502, "search_provider_error", str(error)) from error
        elapsed_ms = (time.monotonic() - started) * 1000
        get_metrics().observe_search(provider=provider.name, results=len(results), elapsed_ms=elapsed_ms)
        logger.info(
            "search_request_completed",
            extra={"provider": provider.name, "query": payload.query, "result_count": len(results)},
        )
        return SearchResponse(
            success=True,
            query=payload.query,
            provider=provider.name,
            limit=len(results),
            data=[
                SearchResultItem(url=item.url, title=item.title, description=item.description)
                for item in results
            ],
        )
    finally:
        readiness_state.end_request()


__all__ = ["search"]
