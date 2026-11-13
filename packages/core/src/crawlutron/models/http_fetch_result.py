"""What a single HTTP fetch returned, before anything is extracted from it."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from safe_url.normalized_url import NormalizedUrl


@dataclass(frozen=True)
class HttpFetchResult:
    """What a single HTTP fetch returned, before anything is extracted from it.

    ``url`` is the logical URL of the producing hop; ``final_url`` the terminal
    logical URL (both logical, never a pinned IP).
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
    # Set-Cookie), kept separately from ``headers`` so ``get_list`` cookie capture survives.
    _raw_headers: Mapping[str, str] | None = None
