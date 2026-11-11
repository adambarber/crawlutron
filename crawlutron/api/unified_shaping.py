"""Narrow re-export shim: the module moved to crawlutron/service (#849)."""

from crawlutron.service.unified_shaping import (
    abuild_unified_result,
    aformat_crawl_item,
    build_unified_result,
    format_crawl_item,
    render_result_to_unified,
)

__all__ = [
    "abuild_unified_result",
    "aformat_crawl_item",
    "build_unified_result",
    "format_crawl_item",
    "render_result_to_unified",
]
