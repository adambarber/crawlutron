"""The crawl-timing block of the capabilities payload."""

from typing import TypedDict


class CrawlTimingRecord(TypedDict):
    """The ``capabilities["crawl_timing"]`` block, spelled where the endpoint writes it."""

    per_url_budget_seconds: int | float
    sync_has_server_time_ceiling: bool
    large_crawl_guidance: str
