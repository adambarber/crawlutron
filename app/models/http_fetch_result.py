"""What a single HTTP fetch returned, before anything is extracted from it."""

from __future__ import annotations

from dataclasses import dataclass

from app.models.normalized_url import NormalizedUrl


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
