"""The mapping core behind ``/v1/map`` (HTTP door stays in api.map_endpoint)."""

import asyncio
import logging
import time

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.crawl_request import CrawlRequest
from crawlutron.models.discovery_config import DiscoveryConfig
from crawlutron.models.host_policy import HostPolicy
from crawlutron.models.map_request import MapRequest
from crawlutron.models.site_map_response import SiteMapPayload
from crawlutron.service.crawl_executor import execute_crawl
from crawlutron.service.main_helpers import record_crawl_completion
from crawlutron.service.site_map import build_site_map
from crawlutron.vocabulary.limits import MAX_DISCOVERED_LINKS

logger = logging.getLogger(__name__)


async def map_site(
    payload: MapRequest,
    settings=None,  # injected via Depends(get_settings)
    shutdown_event: asyncio.Event | None = None,  # injected via Depends(get_shutdown_event)
) -> SiteMapPayload:
    """Map ``payload.root_url`` to its discovered-link frontier.

    Readiness admission (``request_gate``) and caller admission (``admit``) live
    at the door; this runs only the mapping crawl.
    """
    # Built on the model, not a dumped dict: the engine receives what pydantic
    # validated, reshaped into the declared request model discovery forces on.
    policy = payload.policy.model_copy(
        update={
            "discovery": DiscoveryConfig(robots=True, sitemaps=True, feeds=True),
            "limits": payload.policy.limits.model_copy(update={"max_discovered_links": MAX_DISCOVERED_LINKS}),
        }
    )
    crawl = CrawlRequest(url=payload.url, policy=policy)
    _crawl_started = time.monotonic()
    active_settings = settings or CrawlerSettings.from_mapping()
    result = await execute_crawl(
        crawl,
        settings=active_settings,
        shutdown_event=shutdown_event,
    )
    record_crawl_completion(
        result,
        _crawl_started,
        event="map_request_completed",
        logger=logger,
    )
    return build_site_map(result, host_policy=HostPolicy.from_settings(active_settings))


__all__ = ["map_site"]
