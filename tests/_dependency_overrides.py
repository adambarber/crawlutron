"""Shared FastAPI dependency-override installs for the harness apps.

The job-store / settings / shutdown-event overrides are the same three lines in
every harness that builds an app over a router.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import FastAPI

from crawlutron.api.caller_concurrency import CallerConcurrencyTracker
from crawlutron.api.dependencies import get_job_store, get_settings, get_shutdown_event
from crawlutron.config.settings import CrawlerSettings
from crawlutron.storage.job_store import JobStore


def bind_base_overrides(app: FastAPI, *, job_store: JobStore, shutdown_event: asyncio.Event) -> None:
    """Install the job-store / settings / shutdown overrides onto ``app``."""
    app.state.concurrency_tracker = CallerConcurrencyTracker()
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown_event
