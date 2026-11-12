"""The single declaration of crawlutron's module-level feature routers.

One tuple, two consumers: ``main._include_routers`` mounts them and the REST/MCP
parity gate walks them. Factory-built routers and the MCP router are not here.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import APIRouter

#: Mount order preserved from the historical _include_routers body.
_ROUTER_MODULE_NAMES = (
    "crawlutron_rest.api.metrics_router",
    "crawlutron_rest.api.identity_router",
    "crawlutron_rest.api.jobs_router",
    "crawlutron_rest.api.keys_router",
    "crawlutron_rest.api.watch_router",
    "crawlutron_rest.api.crawl_router",
    "crawlutron_rest.api.policy_router",
    "crawlutron_rest.api.admit_router",
)

ROUTER_MODULES: tuple[APIRouter, ...] = tuple(import_module(name).router for name in _ROUTER_MODULE_NAMES)

__all__ = ["ROUTER_MODULES"]
