"""The cached-fetch envelope and retry tally the strategy context carries."""

from __future__ import annotations

from typing import TypedDict


class SerializedResponse(TypedDict):
    """The JSON envelope a fetch result is cached under.

    ``final_url`` is serialized text and re-admitted to the value type on
    rebuild, exactly as a redirect hop re-admits its target."""

    status_code: int
    headers: dict[str, str]
    body_b64: str
    final_url: str
    elapsed_ms: int


class RetryDiagnostics(TypedDict):
    """The per-request retry tally fed into the crawl diagnostics block."""

    attempts: int
    transient_errors: list[str]
    content_cache: str
