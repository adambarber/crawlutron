"""Shared crawl execution path used by ``/v1/crawl`` and ``/v1/map``.

Extracted from ``crawlutron.main`` so execution and handler live apart; the executor
takes ``settings`` and ``shutdown_event`` explicitly (no module globals).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from crawlutron.api.exceptions import browser_pool_unavailable
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.models.taxonomy import as_classification
from crawlutron.orchestration.engine import crawl_request
from crawlutron.storage.result_store import CrawlSpillStore
from crawlutron.transport.browser_pool import PooledBrowserWorker, get_browser_pool
from crawlutron.vocabulary.strategies import BROWSER_STRATEGIES
from crawlutron.vocabulary.taxonomy import CrawlClassification

if TYPE_CHECKING:
    from crawlutron.models.crawl_policy import CrawlPolicy
    from crawlutron.models.crawl_request import CrawlRequest
    from crawlutron.models.result_record import ProducerEnvelope


class CrawlRefusalError(ValueError):
    """An admission refusal the executor raised onward: the refusal reason is a
    member of the vocabulary the deactivation records, not attacker-influenced text.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        # The membership closure: a member keeps its name, a foreign reason
        # degrades to the NAMED unknown_error fallback -- never a cast.
        self.reason = CrawlClassification(as_classification(reason))


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
    url_pairs: list[tuple[str, NormalizedUrl]] | None = None,
    spill_store: CrawlSpillStore | None = None,
) -> ProducerEnvelope:
    """Run a crawl shared by every crawl entry point. ``payload`` is the declared
    :class:`CrawlRequest`; invalid URL 422, missing pool 503 for browser-needed
    chains. crawl_request is the single admission; this adds the pool guard.
    """
    active_settings = settings or CrawlerSettings.from_mapping()
    host_policy = HostPolicy.from_settings(active_settings)
    if payload.url is not None:
        try:
            NormalizedUrl(payload.url, policy=host_policy)
        except ValueError as error:
            raise CrawlRefusalError(str(error)) from error
    pool = get_browser_pool()
    if pool is None and _request_needs_browser(payload.policy):
        raise browser_pool_unavailable()
    pooled_worker = PooledBrowserWorker(pool) if pool is not None else None
    return await crawl_request(
        payload,
        settings=settings,
        url_pairs=url_pairs,
        browser_worker=pooled_worker,
        shutdown_event=shutdown_event,
        spill_store=spill_store,
    )


__all__ = ["CrawlRefusalError", "execute_crawl"]
