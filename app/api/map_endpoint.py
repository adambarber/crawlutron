"""``/v1/map`` endpoint handler.

Map a root URL to its discovered-link frontier (in-page links plus
robots/sitemap/feed URLs). The router in :mod:`app.api.map_router` calls it.
"""

import asyncio
import logging
import time

from app.api.crawl_executor import execute_crawl
from app.api.main_helpers import record_crawl_completion, request_gate
from app.api_models import MapRequest
from app.constants import MAX_DISCOVERED_LINKS
from app.map import build_site_map
from app.settings import CrawlerSettings
from app.validation import HostPolicy

logger = logging.getLogger(__name__)


async def map_site(
    payload: MapRequest,
    settings=None,  # injected via Depends(get_settings)
    shutdown_event: asyncio.Event | None = None,  # injected via Depends(get_shutdown_event)
) -> dict:
    """Map ``payload.root_url`` to its discovered-link frontier."""
    with request_gate("map_request_rejected", logger=logger):
        payload_dict = payload.model_dump(exclude_none=True)
        # Mapping is a discovery operation; force discovery on regardless of caller input.
        policy = payload_dict.setdefault("policy", {})
        disc = dict(policy.get("discovery") or {})
        disc["robots"] = True
        disc["sitemaps"] = True
        disc["feeds"] = True
        policy["discovery"] = disc
        limits = dict(policy.get("limits") or {})
        limits.setdefault("max_discovered_links", MAX_DISCOVERED_LINKS)
        policy["limits"] = limits
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
