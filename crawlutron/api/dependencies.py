"""Shared FastAPI dependencies for cross-module state access.

Routers outside ``crawlutron/main.py`` cannot reach the closure-held app state; the
``*Dep`` ``Annotated`` aliases are the one owner of the wiring.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request

from crawlutron.api.caller_auth import Tracking, get_caller_identity, get_keys_caller
from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.storage.caller_key_store import CallerKeyStore
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore
from crawlutron.storage.watch_store import WatchStore


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


def get_caller_key_store(request: Request) -> CallerKeyStore:
    """FastAPI dependency: return the :class:`CallerKeyStore` for the running app."""
    return request.app.state.caller_key_store


def get_tracking(request: Request, job_store: JobStoreDep) -> Tracking:
    """FastAPI dependency: the one per-caller register plus the job store for admission."""
    tracker = getattr(request.app.state, "concurrency_tracker", None)
    return Tracking(job_store=job_store, concurrency_tracker=tracker)


# One declaration per provider, Annotated rather than a default so the dependency is
# declared: an unannotated provider makes the alias, and every endpoint, carry Any.
CallerKeyStoreDep = Annotated[CallerKeyStore, Depends(get_caller_key_store)]
CallerDep = Annotated[CallerIdentity, Depends(get_caller_identity)]
KeysCallerDep = Annotated[CallerIdentity, Depends(get_keys_caller)]
SettingsDep = Annotated[CrawlerSettings, Depends(get_settings)]
ShutdownEventDep = Annotated[asyncio.Event, Depends(get_shutdown_event)]
JobStoreDep = Annotated[JobStore, Depends(get_job_store)]
ResultStoreDep = Annotated[CrawlResultStore, Depends(get_result_store)]
WatchStoreDep = Annotated[WatchStore, Depends(get_watch_store)]
TrackingDep = Annotated[Tracking, Depends(get_tracking)]


__all__ = [
    "CallerDep",
    "KeysCallerDep",
    "CallerKeyStoreDep",
    "JobStoreDep",
    "ResultStoreDep",
    "SettingsDep",
    "ShutdownEventDep",
    "WatchStoreDep",
    "TrackingDep",
    "get_caller_key_store",
    "get_job_store",
    "get_result_store",
    "get_settings",
    "get_shutdown_event",
    "get_tracking",
    "get_watch_store",
]
