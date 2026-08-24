"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import inspect
from typing import Any

from app.crawl.strategies.http import _decode_body, _decompress_body


# Discovery bodies (robots.txt / sitemap.xml) are attacker-influenced; cap their
# decompressed size so a hostile origin cannot OOM the service via a gzip bomb.
_MAX_DISCOVERY_BODY_BYTES = 8 * 1024 * 1024


async def _fetch_and_decode(
    fetcher: Any,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> str | None:
    """Fetch a URL and return decoded text body, or None on failure."""
    from app.crawl.decompression import DecompressionTooLargeError

    try:
        resp = fetcher(url, headers, timeout_seconds)
        if inspect.iscoroutine(resp):
            resp = await resp
        if resp.status_code == 200:
            resp_headers = {key.lower(): value for key, value in resp.headers.items()}
            try:
                body = _decompress_body(
                    resp.body,
                    resp_headers.get("content-encoding", ""),
                    max_output_bytes=_MAX_DISCOVERY_BODY_BYTES,
                )
            except DecompressionTooLargeError:
                return None
            return _decode_body(body, resp_headers.get("content-type"))
    except Exception:  # noqa: BLE001
        pass
    return None
