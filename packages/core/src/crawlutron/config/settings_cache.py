"""Process-wide settings cache: one parse of os.environ per process.

The cached door is the production entry point; tests build settings directly
through ``CrawlerSettings.from_mapping`` so per-test env patches still apply.
"""

from __future__ import annotations

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

    Test seam: lets each test read fresh env instead of a stale cache, since
    tests monkeypatch CRAWLUTRON_* variables that production code reads.
    """
    global _CACHED_SETTINGS
    _CACHED_SETTINGS = None
