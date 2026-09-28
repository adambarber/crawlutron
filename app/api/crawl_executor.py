"""Shared crawl execution path used by ``/v1/crawl`` and ``/v1/map``.

Extracted from ``app/main.py`` so the crawl-execution logic and the
endpoint handlers live in different modules. The executor takes the
caller-supplied ``settings`` and ``shutdown_event`` explicitly (no
module-level globals) so it can be invoked from any router.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.api.exceptions import browser_pool_unavailable
from app.constants import BROWSER_STRATEGIES
from app.crawl.engine import crawl_request
from app.crawl.policy import read_policy
from app.crawl.strategies.browser_pool import PooledBrowserWorker, get_browser_pool
from app.settings import CrawlerSettings
from app.validation import HostPolicy


def _request_needs_browser(payload_dict: dict) -> bool:
    """Return True when the request's strategy chain includes a browser step.

    Used to defer the ``browser_pool_unavailable`` 503 to only requests that
    actually need the browser pool, so HTTP-only crawls can succeed during
    lifespan startup or after a pool-init failure (adversarial review
    F-browser-pool-none).

    The chain is read through ``read_policy`` rather than ``payload["policy"]
    .get("strategies")`` because the job path puts a ``CrawlPolicy`` in that slot
    and the crawl path puts a dict. Reading the dict shape made ``.get`` raise
    ``AttributeError`` on the model, and the old handler swallowed it into
    ``False`` -- so the guard silently stopped guarding on exactly the path it
    exists for: a whole-site job with the pool down burned its HTTP strategies,
    recorded ``browser_worker_unavailable`` per URL, and completed as a
    partially-failed job instead of being refused. ``read_policy`` is total, so
    there is no exception to swallow here at all, and an absent policy answers
    with the declared default chain -- which does include browser strategies,
    so "no policy given" correctly means "the browser pool is needed".
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
) -> dict[str, Any]:
    """Run a crawl shared by ``/v1/crawl`` and ``/v1/map``.

    URL pre-validation: an invalid URL surfaces as a 422 (via the caller's
    ``except ValueError``) instead of a misleading 503 from the pool guard.

    Pool guard: only raises ``browser_pool_unavailable`` 503 when the pool
    is None AND the request actually needs a browser strategy.

    ``urls`` forwards the caller's already-gated batch (tier-2 T3) so the
    engine skips re-deriving it; the cap still applies there.
    """
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    target_url = payload_dict.get("url")
    if isinstance(target_url, str):
        from app.validation import normalize_public_http_url

        try:
            normalize_public_http_url(target_url, policy=host_policy)
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
