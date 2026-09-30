"""``/v1/render`` endpoint handler.

Backward-compatible browser-render endpoint that wraps
:func:`app.api.render.render_request` with the readiness gate and the
unified response shape. Extracted from the previous monolith in
``app/main.py`` so the handler logic and the FastAPI URL→handler mapping
live in different modules; the router in :mod:`app.api.render_router`
calls it.
"""

import logging

from fastapi import HTTPException

from app.api.exceptions import browser_pool_unavailable, http_error
from app.api.main_helpers import request_gate
from app.api.render import render_request
from app.api.response_shaping import _aunified_response_from_result, render_result_to_unified
from app.api_models import (
    RenderRequest,
)
from app.api_responses import (
    UnifiedCrawlResponse,
)
from app.crawl.strategies.browser_pool import (
    BrowserPoolExhaustedError,
    PooledBrowserWorker,
    get_browser_pool,
)
from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


def _render_rejection(error: ValueError) -> HTTPException:
    """``/v1/render``'s reject envelope: 400, and the ValueError text as the message.

    ``code`` is the machine-readable contract clients branch on and the
    ValueError text stays as the message. Emitting the text as the code made
    every new rejection reason a new, unenumerated wire value, so this is a
    named factory handed to the shared gate rather than a literal at a raise
    the handler no longer has.
    """
    return http_error(400, "validation_error", str(error))


async def render(payload: RenderRequest, settings: CrawlerSettings) -> UnifiedCrawlResponse:
    """Render ``payload`` URLs with the pooled browser worker."""
    with request_gate("render_request_rejected", logger=logger, reject=_render_rejection):
        # Every render request needs the browser by definition: fail clean
        # with a 503 when the pool is down (startup/shutdown window)
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
        # The render results go through the SAME per-item shape guard
        # /v1/crawl uses (``render_result_to_unified`` reaches it as the
        # adaptation step), so a result dict the adapter cannot read degrades
        # to a terminal failure for that item instead of 500ing the response.
        return await _aunified_response_from_result(result, adapt=render_result_to_unified)


__all__ = ["render"]
