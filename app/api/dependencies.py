"""Shared FastAPI dependencies for cross-module state access.

The :class:`FastAPI` instance stores per-app state on ``app.state``
(lifespan-attached ``crawler_settings`` and ``shutdown_event``). Routers
defined in modules outside ``app/main.py`` cannot reach into the closure
where those values live, so they use the ``Depends(get_settings)`` /
``Depends(get_shutdown_event)`` providers below.
"""

from __future__ import annotations

import asyncio

from fastapi import Request


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


__all__ = ["get_settings", "get_shutdown_event", "get_job_store", "get_result_store"]
