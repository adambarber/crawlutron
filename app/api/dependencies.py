"""Shared FastAPI dependencies for cross-module state access.

Routers outside ``app/main.py`` cannot reach the closure-held app state; the
``*Dep`` ``Annotated`` aliases are the one owner of the wiring.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request

from app.config.settings import CrawlerSettings
from app.storage.job_store import JobStore
from app.storage.result_store import CrawlResultStore
from app.storage.watch_store import WatchStore


def get_settings(request: Request) -> CrawlerSettings:
    """FastAPI dependency: return the active :class:`CrawlerSettings` instance."""
    return request.app.state.crawler_settings


def get_shutdown_event(request: Request) -> asyncio.Event:
    """FastAPI dependency: return the shutdown-event handle for the running app."""
    return request.app.state.shutdown_event


def get_job_store(request: Request) -> JobStore:
    """FastAPI dependency: return the :class:`JobStore` for the running app."""
    return request.app.state.job_store


def get_result_store(request: Request) -> CrawlResultStore:
    """FastAPI dependency: return the :class:`CrawlResultStore` for the running app."""
    return request.app.state.result_store


def get_watch_store(request: Request) -> WatchStore:
    """FastAPI dependency: return the :class:`WatchStore` for the running app."""
    return request.app.state.watch_store


# One declaration per provider, Annotated rather than a default so the dependency is
# declared: an unannotated provider makes the alias, and every endpoint, carry Any.
SettingsDep = Annotated[CrawlerSettings, Depends(get_settings)]
ShutdownEventDep = Annotated[asyncio.Event, Depends(get_shutdown_event)]
JobStoreDep = Annotated[JobStore, Depends(get_job_store)]
ResultStoreDep = Annotated[CrawlResultStore, Depends(get_result_store)]
WatchStoreDep = Annotated[WatchStore, Depends(get_watch_store)]


__all__ = [
    "JobStoreDep",
    "ResultStoreDep",
    "SettingsDep",
    "ShutdownEventDep",
    "WatchStoreDep",
    "get_job_store",
    "get_result_store",
    "get_settings",
    "get_shutdown_event",
    "get_watch_store",
]
