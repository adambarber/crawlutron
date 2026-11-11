"""Configuration admission: read the active :class:`CrawlerSettings` off app.state.

FastAPI dependency semantics are api-plane, but the getter is plain
configuration admission, so it lives here and api imports it.
"""

from __future__ import annotations

from typing import Protocol

from crawlutron.config.settings import CrawlerSettings

__all__ = ["AppSettingsCarrier", "get_settings"]


class AppSettingsCarrier(Protocol):
    """The state carrier the settings ride (FastAPI's app.state satisfies this)."""

    crawler_settings: CrawlerSettings


def get_settings(app_state: AppSettingsCarrier) -> CrawlerSettings:
    """Return the active :class:`CrawlerSettings` from the carrier's state."""
    return app_state.crawler_settings
