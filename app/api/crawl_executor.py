"""Shared crawl execution path used by ``/v1/crawl`` and ``/v1/map``.

Extracted from ``app.main`` so execution and handler live apart; the executor
takes ``settings`` and ``shutdown_event`` explicitly (no module globals).
"""

from __future__ import annotations

import asyncio

from app.api.exceptions import browser_pool_unavailable
from app.config.settings import CrawlerSettings
from app.models.crawl_policy import CrawlPolicy
from app.models.crawl_request import CrawlRequest
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl
from app.models.result_record import ProducerEnvelope
from app.orchestration.engine import crawl_request
from app.transport.browser_pool import PooledBrowserWorker, get_browser_pool
from app.vocabulary.strategies import BROWSER_STRATEGIES


def _request_needs_browser(policy: CrawlPolicy) -> bool:
    """True when the request's strategy chain includes a browser step.
    Defers the ``browser_pool_unavailable`` 503 to only requests that need it,
    reading the chain off the policy's strategies (absent = default, browser).
    """
    return any(strategy in BROWSER_STRATEGIES for strategy in policy.strategies)


async def execute_crawl(
    payload: CrawlRequest,
    *,
    settings,
    shutdown_event: asyncio.Event | None,
    urls: list[str] | None = None,
) -> ProducerEnvelope:
    """Run a crawl shared by every crawl entry point. ``payload`` is the declared
    :class:`CrawlRequest` model; an invalid URL surfaces as 422, a missing pool as
    503 only for a browser-needed chain. ``urls`` forwards the gated batch.
    """
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    if payload.url is not None:
        try:
            NormalizedUrl(payload.url, policy=host_policy)
        except ValueError:
            return await crawl_request(
                payload,
                settings=active_settings,
                urls=urls,
                browser_worker=None,
                shutdown_event=shutdown_event,
            )
    pool = get_browser_pool()
    if pool is None and _request_needs_browser(payload.policy):
        raise browser_pool_unavailable()
    pooled_worker = PooledBrowserWorker(pool) if pool is not None else None
    return await crawl_request(
        payload,
        settings=settings,
        urls=urls,
        browser_worker=pooled_worker,
        shutdown_event=shutdown_event,
    )


__all__ = ["_request_needs_browser", "execute_crawl"]
