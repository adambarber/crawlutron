"""Test helpers for the extraction pipeline (post-cutover).

Only the helpers still needed by surviving tests are kept; they source from
``crawlutron.extraction.discovery`` and ``crawlutron.transport.fetchers``.
"""

from __future__ import annotations

__all__ = ["default_http_fetcher", "extract_discovered_links", "pin_selector_pool"]

from crawlutron.extraction.discovery import (
    extract_discovered_links as _production_extract_discovered_links,
)
from crawlutron.transport.fetchers import default_http_fetcher as _production_default_http_fetcher


def pin_selector_pool(monkeypatch):
    """No-op shim for the unused ``four_worker_selector_pool`` fixture.

    The doc pool no longer exists, so there is nothing to shut down or rebuild.
    """
    monkeypatch.setenv("CRAWLUTRON_EXTRACTION_THREADS", "4")


def extract_discovered_links(html, source_url, *, max_links=250, tree=None):
    return _production_extract_discovered_links(html, source_url, max_links=max_links, tree=tree)


async def default_http_fetcher(*args, **kwargs):
    return await _production_default_http_fetcher(*args, **kwargs)
