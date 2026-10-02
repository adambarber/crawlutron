"""``/v1/map`` endpoint handler.

Map a root URL to its discovered-link frontier (in-page links plus
robots/sitemap/feed URLs). The router in :mod:`app.api.map_router` calls it.
"""

import asyncio
import logging
import time

from app.api.crawl_executor import execute_crawl
from app.api.main_helpers import record_crawl_completion, request_gate
from app.api_models import DiscoveryConfig
from app.constants import MAX_DISCOVERED_LINKS
from app.map import build_site_map
from app.models.host_policy import HostPolicy
from app.models.map_request import MapRequest
from app.settings import CrawlerSettings

logger = logging.getLogger(__name__)


async def map_site(
    payload: MapRequest,
    settings=None,  # injected via Depends(get_settings)
    shutdown_event: asyncio.Event | None = None,  # injected via Depends(get_shutdown_event)
) -> dict:
    """Map ``payload.root_url`` to its discovered-link frontier."""
    with request_gate("map_request_rejected", logger=logger):
        # Mapping is a discovery operation, so the caller's discovery block is
        # overridden rather than honoured -- expressed on the model, not on the
        # dumped dict. The dict was the last place a policy was still mutated
        # key-by-key, and it only worked because ``execute_crawl`` reads through
        # the shared read edge; building the override on the model means the
        # engine receives a value pydantic itself validated, and there is no
        # second spelling of "map forces discovery" left to drift.
        policy = payload.policy.model_copy(
            update={
                "discovery": DiscoveryConfig(robots=True, sitemaps=True, feeds=True),
                "limits": payload.policy.limits.model_copy(
                    update={"max_discovered_links": MAX_DISCOVERED_LINKS}
                ),
            }
        )
        payload_dict = payload.model_dump(exclude_none=True)
        payload_dict["policy"] = policy
        _crawl_started = time.monotonic()
        active_settings = settings or CrawlerSettings.from_mapping()
        result = await execute_crawl(
            payload_dict,
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
