"""The ``diagnostics["discovery"]`` block: which discovery lists the page yielded."""

from typing import TypedDict


class DiscoveryDiagnostics(TypedDict):
    """The ``diagnostics["discovery"]`` block: which discovery lists the page yielded."""

    sitemap_urls: list[str]
    feeds: list[str]
