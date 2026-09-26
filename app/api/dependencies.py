"""Shared FastAPI dependencies for cross-module state access.

The :class:`FastAPI` instance stores per-app state on ``app.state``
(lifespan-attached ``crawler_settings`` and ``shutdown_event``). Routers
defined in modules outside ``app/main.py`` cannot reach into the closure
where those values live, so they use the ``Depends(get_settings)`` /
``Depends(get_shutdown_event)`` providers below.

The ``*Dep`` aliases are those same providers in ``Annotated`` form, and they
are the one owner of the wiring. An endpoint signature names the alias instead
of repeating ``= Depends(get_x)`` because a repeated dependency list is a
list that can drift: ``create_job`` and ``control_job`` need the IDENTICAL
triple (nothing about creating a job and controlling one needs different state),
which used to be two hand-written lists that could disagree. That the two sets
are still equal is checked against the live signatures, not asserted here in a
second list that could rot.
"""

from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import Depends, Request

from app.crawl.job_store import JobStore
from app.settings import CrawlerSettings


def get_settings(request: Request):
    """FastAPI dependency: return the active :class:`CrawlerSettings` instance."""
    return request.app.state.crawler_settings


def get_shutdown_event(request: Request) -> asyncio.Event:
    """FastAPI dependency: return the shutdown-event handle for the running app."""
    return request.app.state.shutdown_event


def get_job_store(request: Request):
    """FastAPI dependency: return the :class:`JobStore` for the running app."""
    return request.app.state.job_store


def get_result_store(request: Request):
    """FastAPI dependency: return the :class:`ResultStore` for the running app."""
    return request.app.state.result_store


# One declaration per provider: the annotation an endpoint parameter carries.
# ``Annotated`` rather than a default value, so the dependency is a declared
# part of the signature instead of an invisible default a reader has to notice.
SettingsDep = Annotated[CrawlerSettings, Depends(get_settings)]
ShutdownEventDep = Annotated[asyncio.Event, Depends(get_shutdown_event)]
JobStoreDep = Annotated[JobStore, Depends(get_job_store)]


__all__ = ["JobStoreDep", "SettingsDep", "ShutdownEventDep", "get_job_store", "get_result_store",
           "get_settings", "get_shutdown_event"]
