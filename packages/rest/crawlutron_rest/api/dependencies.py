"""Shared FastAPI dependencies for cross-module state access.

Routers outside ``crawlutron/main.py`` cannot reach the closure-held app state; the
``*Dep`` ``Annotated`` aliases are the one owner of the wiring.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request

from crawlutron.config.settings import CrawlerSettings
from crawlutron.models.caller_identity import CallerIdentity
from crawlutron.service.caller_auth import CallerQuotaAdmission
from crawlutron.service.caller_concurrency import CallerConcurrencyTracker
from crawlutron.service.errors import DomainError
from crawlutron.service.settings_access import get_settings as get_app_settings
from crawlutron.storage.caller_key_store import CallerKeyStore
from crawlutron.storage.job_store import JobStore
from crawlutron.storage.result_store import CrawlResultStore, CrawlSpillStore
from crawlutron.storage.watch_store import WatchStore
from crawlutron_rest.api.caller_auth import Tracking, get_caller_identity, get_keys_caller


def get_settings(request: Request) -> CrawlerSettings:
    """FastAPI dependency: the active :class:`CrawlerSettings` via the service getter."""
    return get_app_settings(request.app.state)


def get_shutdown_event(request: Request) -> asyncio.Event:
    """FastAPI dependency: return the shutdown-event handle for the running app."""
    return request.app.state.shutdown_event


def get_job_store(request: Request) -> JobStore:
    """FastAPI dependency: return the :class:`JobStore` for the running app."""
    return request.app.state.job_store


def get_result_store(request: Request) -> CrawlResultStore:
    """FastAPI dependency: return the :class:`CrawlResultStore` for the running app."""
    return request.app.state.result_store


def get_spill_store(request: Request) -> CrawlSpillStore | None:
    """FastAPI dependency: the running app's :class:`CrawlSpillStore`, or None
    when the app wires none -- the engine then spills to its own private
    per-crawl store, the same per-crawl lifetime the deleted file spill had."""
    return getattr(request.app.state, "spill_store", None)


def get_watch_store(request: Request) -> WatchStore:
    """FastAPI dependency: return the :class:`WatchStore` for the running app."""
    return request.app.state.watch_store


def get_caller_key_store(request: Request) -> CallerKeyStore:
    """FastAPI dependency: return the :class:`CallerKeyStore` for the running app."""
    return request.app.state.caller_key_store


def get_concurrency_tracker(request: Request) -> CallerConcurrencyTracker:
    """FastAPI dependency: the per-caller concurrency tracker the app stashed.

    The stash is load-bearing: an app that forgot the attribute is a wiring
    defect and is refused here, never silently degraded to rows-only quota.
    """
    tracker = getattr(request.app.state, "concurrency_tracker", None)
    if tracker is None:
        raise DomainError(
            500, "tracker_missing", "the service's concurrency tracker is not wired into app.state"
        )
    return tracker


def get_tracking(request: Request, job_store: JobStoreDep, tracker: TrackerDep) -> Tracking:
    """FastAPI dependency: the one per-caller register plus the job store for admission."""
    return Tracking(job_store=job_store, concurrency_tracker=tracker)


def get_caller_quota(request: Request) -> CallerQuotaAdmission:
    """FastAPI dependency: the caller-quota admission port wired at construction."""
    admission = getattr(request.app.state, "caller_quota_admission", None)
    if admission is None:
        raise DomainError(
            500,
            "caller_quota_unwired",
            "the caller-quota admission port is not wired into app.state",
        )
    return admission


# One declaration per provider, Annotated rather than a default so the dependency is
# declared: an unannotated provider makes the alias, and every endpoint, carry Any.
CallerKeyStoreDep = Annotated[CallerKeyStore, Depends(get_caller_key_store)]
CallerDep = Annotated[CallerIdentity, Depends(get_caller_identity)]
KeysCallerDep = Annotated[CallerIdentity, Depends(get_keys_caller)]
SettingsDep = Annotated[CrawlerSettings, Depends(get_settings)]
ShutdownEventDep = Annotated[asyncio.Event, Depends(get_shutdown_event)]
JobStoreDep = Annotated[JobStore, Depends(get_job_store)]
ResultStoreDep = Annotated[CrawlResultStore, Depends(get_result_store)]
SpillStoreDep = Annotated[CrawlSpillStore | None, Depends(get_spill_store)]
WatchStoreDep = Annotated[WatchStore, Depends(get_watch_store)]
TrackingDep = Annotated[Tracking, Depends(get_tracking)]
AdmissionDep = Annotated[CallerQuotaAdmission, Depends(get_caller_quota)]
TrackerDep = Annotated[CallerConcurrencyTracker, Depends(get_concurrency_tracker)]


__all__ = [
    "AdmissionDep",
    "CallerDep",
    "KeysCallerDep",
    "CallerKeyStoreDep",
    "JobStoreDep",
    "ResultStoreDep",
    "SettingsDep",
    "ShutdownEventDep",
    "SpillStoreDep",
    "TrackerDep",
    "WatchStoreDep",
    "TrackingDep",
    "get_caller_key_store",
    "get_caller_quota",
    "get_concurrency_tracker",
    "get_job_store",
    "get_result_store",
    "get_spill_store",
    "get_settings",
    "get_shutdown_event",
    "get_tracking",
    "get_watch_store",
]
