"""``/v1/map`` endpoint handler.

Map a root URL to its discovered-link frontier (in-page links plus
robots/sitemap/feed URLs). The handler is exposed as a plain async
function; the router in :mod:`app.api.map_router` calls it. main.py wires
the router into the FastAPI app via ``include_router``.
"""

from __future__ import annotations

import asyncio
import logging
import time

from fastapi import HTTPException

from app.api.crawl_executor import execute_crawl
from app.api_models import MapRequest
from app.constants import MAX_DISCOVERED_LINKS
from app.health.readiness import READINESS_STATE as readiness_state
from app.map import build_site_map
from app.metrics import get_metrics

logger = logging.getLogger(__name__)


async def map_site(
    payload: MapRequest,
    settings=None,  # injected via Depends(get_settings)
    shutdown_event: asyncio.Event = None,  # injected via Depends(get_shutdown_event)
) -> dict:
    """Map ``payload.root_url`` to its discovered-link frontier."""
    readiness_state.begin_request()
    try:
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
        result = await execute_crawl(
            payload_dict,
            settings=settings,
            shutdown_event=shutdown_event,
        )
        get_metrics().observe_crawl(
            results=result.get("results", []),
            elapsed_ms=(time.monotonic() - _crawl_started) * 1000,
        )
        logger.info(
            "map_request_completed",
            extra={"url_count": len(result.get("results", []))},
        )
        return build_site_map(result)
    except ValueError as error:
        logger.warning("map_request_rejected", extra={"error": str(error)})
        raise HTTPException(
            status_code=422,
            detail={
                "code": "validation_error",
                "message": "Request validation failed",
                "details": {"errors": [{"msg": str(error)}]},
            },
        ) from error
    finally:
        readiness_state.end_request()


__all__ = ["map_site"]
