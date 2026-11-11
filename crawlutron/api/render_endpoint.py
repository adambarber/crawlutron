"""``/v1/render`` endpoint handler.

Backward-compatible browser-render endpoint wrapping
:func:`crawlutron.api.render.render_request` with the unified shape.
"""

import logging

from crawlutron.api.exceptions import browser_pool_unavailable
from crawlutron.api.render import render_request
from crawlutron.api.response_shaping import arender_response_from_result
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.browser_pool_exhausted import BrowserPoolExhaustedError
from crawlutron.models.render_request import RenderRequest
from crawlutron.models.unified_crawl_response import UnifiedCrawlResponse
from crawlutron.service.errors import DomainError, ValidationRefusal
from crawlutron.transport.browser_pool import (
    PooledBrowserWorker,
    get_browser_pool,
)

logger = logging.getLogger(__name__)


def render_rejection(error: ValueError) -> DomainError:
    """``/v1/render``'s reject envelope: 400, ValueError text as the message.

    ``code`` is the machine-readable contract clients branch on, so this is a named
    factory handed to the gate, not a literal at a raise.
    """
    return ValidationRefusal("validation_error", str(error), status=400)


async def render(payload: RenderRequest, settings: CrawlerSettings) -> UnifiedCrawlResponse:
    """Render ``payload`` URLs with the pooled browser worker.

    Readiness and caller admission live at the door; refusals propagate as ``ValueError``.
    """
    # An absent pool refuses 503 (startup/shutdown) rather than an AttributeError 500.
    pool = get_browser_pool()
    if pool is None:
        raise browser_pool_unavailable()
    pooled_worker = PooledBrowserWorker(pool)
    try:
        result = await render_request(
            payload,
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
    # the SAME per-item shape guard /v1/crawl uses, so a result the adapter cannot read
    # degrades to a terminal failure for that item instead of 500ing the response.
    return await arender_response_from_result(result)


__all__ = ["render", "render_rejection"]
