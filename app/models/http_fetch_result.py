"""What a single HTTP fetch returned, before anything is extracted from it."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class HttpFetchResult:
    url: str
    final_url: str
    status_code: int
    headers: dict[str, str]
    body: bytes
    elapsed_ms: int
    # True when a Content-Encoding indicated compression but decompression
    # failed silently (garbage bytes delivered as-is). Downstream consumers
    # downgrade quality and flag diagnostics instead of trusting the text.
    decompression_failed: bool = False
