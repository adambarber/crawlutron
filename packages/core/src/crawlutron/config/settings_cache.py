"""Process-wide settings cache: one parse of os.environ per process.

The cached door is the production entry point; tests build settings directly
through ``CrawlerSettings.from_mapping`` so per-test env patches still apply.
"""

from __future__ import annotations

import os

from crawlutron.config.settings import CrawlerSettings

_CACHED_SETTINGS: CrawlerSettings | None = None


def get_cached_settings() -> CrawlerSettings:
    """Return the cached settings, parsing os.environ once.

    One parse per process; later calls return the same object.
    """
    global _CACHED_SETTINGS
    if _CACHED_SETTINGS is None:
        _CACHED_SETTINGS = CrawlerSettings.from_mapping()
    return _CACHED_SETTINGS


def reset_cached_settings() -> None:
    """Reset the cached settings so the next call re-reads os.environ.

    Test-only seam: production cannot re-parse mid-process. Tests enable it
    with CRAWLUTRON_TEST_ALLOW_SETTINGS_RESET=1.
    """
    if os.environ.get("CRAWLUTRON_TEST_ALLOW_SETTINGS_RESET") != "1":
        raise RuntimeError("settings cache reset is test-only; set CRAWLUTRON_TEST_ALLOW_SETTINGS_RESET=1")
    global _CACHED_SETTINGS
    _CACHED_SETTINGS = None
