"""``/v1/render`` endpoint handler.

Backward-compatible browser-render endpoint that wraps
:func:`app.api.render.render_request` with the readiness gate and the
unified response shape. Extracted from the previous monolith in
``app/main.py`` so the handler logic and the FastAPI URL→handler mapping
live in different modules.

The handler is exposed as a plain async function; the router in
:mod:`app.api.render_router` calls it. main.py wires the router into the
FastAPI app via ``include_router``.
"""

from __future__ import annotations

import logging

from app.api.exceptions import browser_pool_unavailable, http_error
from app.api.render import render_request
from app.api.response_shaping import (
    build_unified_crawl_response,
    render_result_to_unified,
)
from app.api_models import POLICY_SCHEMA_VERSION, RenderRequest, UnifiedCrawlResponse
from app.crawl.strategies.browser_pool import (
    BrowserPoolExhaustedError,
    PooledBrowserWorker,
    get_browser_pool,
)
from app.health.readiness import READINESS_STATE as readiness_state
from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


async def render(payload: RenderRequest, settings: CrawlerSettings) -> UnifiedCrawlResponse:
    """Render ``payload`` URLs with the pooled browser worker."""
    readiness_state.begin_request()
    try:
        # Every render request needs the browser by definition: fail clean
        # with a 503 when the pool is down (startup/shutdown window, M13)
        # instead of an AttributeError 500 from inside the worker.
        pool = get_browser_pool()
        if pool is None:
            raise browser_pool_unavailable()
        pooled_worker = PooledBrowserWorker(pool)
        try:
            result = await render_request(
                payload.model_dump(exclude_none=True),
                settings=settings,
                worker=pooled_worker,
            )
        except BrowserPoolExhaustedError as error:
            logger.warning("render_request_pool_exhausted", extra={"error": str(error)})
            raise browser_pool_unavailable(str(error)) from error
        logger.info(
            "render_request_completed",
            extra={"url_count": len(result.get("results", []))},
        )
        return build_unified_crawl_response(
            [render_result_to_unified(item) for item in result.get("results", [])],
            request_id=result["request_id"],
            submitted_count=result["submitted_count"],
            policy_schema_version=POLICY_SCHEMA_VERSION,
        )
    except ValueError as error:
        logger.warning("render_request_rejected", extra={"error": str(error)})
        raise http_error(400, str(error), str(error)) from error
    finally:
        readiness_state.end_request()


__all__ = ["render"]
