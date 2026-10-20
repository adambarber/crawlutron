"""Shared FastAPI dependency-override installs for the harness apps.

The job-store / settings / shutdown-event overrides are the same three lines in
every harness that builds an app over a router.
"""

from __future__ import annotations

from typing import Any

from app.api.dependencies import get_job_store, get_settings, get_shutdown_event
from app.config.settings import CrawlerSettings


def bind_base_overrides(app: Any, *, job_store: Any, shutdown_event: Any) -> None:
    """Install the job-store / settings / shutdown overrides onto ``app``."""
    app.dependency_overrides[get_job_store] = lambda: job_store
    app.dependency_overrides[get_settings] = lambda: CrawlerSettings.from_mapping({})
    app.dependency_overrides[get_shutdown_event] = lambda: shutdown_event
