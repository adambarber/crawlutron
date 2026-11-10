"""What a single HTTP fetch returned, before anything is extracted from it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from crawlutron.models.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class HttpFetchResult:
    """What a single HTTP fetch returned, before anything is extracted from it.

    ``url`` is the logical URL of the hop that produced this result -- for a
    terminal result that is the terminal-hop logical URL (post-#685), not the
    original admitted URL after redirects. ``final_url`` is the logical final
    URL and is unchanged by redirect following; both are logical URLs, never a
    pinned IP/connect address.
    """

    url: NormalizedUrl
    final_url: NormalizedUrl
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    # failed silently (garbage bytes delivered as-is). Downstream consumers downgrade
    # quality and flag diagnostics instead of trusting the text.
    decompression_failed: bool = False
    # Raw transport headers (a multidict preserving duplicate names, e.g. repeated
    # Set-Cookie). Kept separately from ``headers`` (a dict that comma-joins
    # duplicates) so cookie capture via ``get_list`` survives the walk. Only the
    # redirect walker reads this field; consumers read ``headers``.
    _raw_headers: Mapping[str, str] | None = None
