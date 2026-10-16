"""Shared crawl execution path used by ``/v1/crawl`` and ``/v1/map``.

Extracted from ``app.main`` so execution and handler live apart; the executor
takes ``settings`` and ``shutdown_event`` explicitly (no module globals).
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.admission.policy import read_policy
from app.api.exceptions import browser_pool_unavailable
from app.config.settings import CrawlerSettings
from app.models.host_policy import HostPolicy
from app.models.result_record import ProducerEnvelope
from app.orchestration.engine import crawl_request
from app.transport.browser_pool import PooledBrowserWorker, get_browser_pool
from app.vocabulary.strategies import BROWSER_STRATEGIES


def _request_needs_browser(payload_dict: dict) -> bool:
    """True when the request's strategy chain includes a browser step.

        Defers the ``browser_pool_unavailable`` 503 to only requests that need it, reading
    the chain via ``read_policy`` (total) -- absent policy = default chain, which has browser steps.
    """
    return any(
        strategy in BROWSER_STRATEGIES for strategy in read_policy(payload_dict.get("policy")).strategies
    )


async def execute_crawl(
    payload_dict: dict[str, Any],
    *,
    settings,
    shutdown_event: asyncio.Event | None,
    urls: list[str] | None = None,
) -> ProducerEnvelope:
    """Run a crawl shared by ``/v1/crawl`` and ``/v1/map``.

    An invalid URL surfaces as 422 (not a 503 from the pool guard); a 503 only fires
    when the pool is None and a browser is needed. ``urls`` forwards the gated batch.
    """
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    target_url = payload_dict.get("url")
    if isinstance(target_url, str):
        from app.models.normalized_url import NormalizedUrl

        try:
            NormalizedUrl(target_url, policy=host_policy)
        except ValueError:
            return await crawl_request(
                payload_dict,
                settings=active_settings,
                urls=urls,
                browser_worker=None,
                shutdown_event=shutdown_event,
            )
    pool = get_browser_pool()
    if pool is None and _request_needs_browser(payload_dict):
        raise browser_pool_unavailable()
    pooled_worker = PooledBrowserWorker(pool) if pool is not None else None
    return await crawl_request(
        payload_dict,
        settings=settings,
        urls=urls,
        browser_worker=pooled_worker,
        shutdown_event=shutdown_event,
    )


__all__ = ["_request_needs_browser", "execute_crawl"]
