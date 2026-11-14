"""The ``diagnostics["discovery"]`` block: which discovery lists the page yielded."""

from typing import NotRequired, TypedDict


class DiscoveryDiagnostics(TypedDict):
    """The ``diagnostics["discovery"]`` block: which discovery lists the page yielded."""

    sitemap_urls: list[str]
    feeds: list[str]
    fetch_count: NotRequired[int]
    directive_drop_count: NotRequired[int]
