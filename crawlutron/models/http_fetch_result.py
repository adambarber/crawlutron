"""What a single HTTP fetch returned, before anything is extracted from it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from crawlutron.models.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class HttpFetchResult:
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
