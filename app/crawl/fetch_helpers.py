"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import asyncio
import inspect
import time
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


_MAX_SITEMAP_DEPTH = 3
_SITEMAP_CACHE_TTL = 3600.0
_SITEMAP_CACHE: "dict[str, tuple[float, set]]" = {}
_MAX_SITEMAP_URLS = 10_000


async def fetch_sitemap_urls(
    fetcher: Any,
    sitemap_url: str,
    timeout_seconds: int,
    *,
    max_depth: int = _MAX_SITEMAP_DEPTH,
    max_urls: int = _MAX_SITEMAP_URLS,
) -> set[tuple[str, str | None]]:
    """Recursively resolve a sitemap URL into ``(loc, lastmod)`` pairs.

    Handles sitemap indexes (``<sitemapindex>`` pointing at child sitemaps) up
    to ``max_depth`` levels, and caps total collected URLs at ``max_urls`` so a
    hostile or enormous index cannot run away.

    Each collected entry is a ``(url, lastmod)`` tuple where ``lastmod`` is the
    sitemap ``<lastmod>`` for that URL when present (used to skip re-fetching
    unchanged pages, #1); it is ``None`` when the entry has no ``<lastmod>``.
    """
    import re as _re

    loc_pattern = _re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", _re.IGNORECASE)
    lastmod_re = _re.compile(r"<lastmod>\s*([^<]+)\s*</lastmod>", _re.IGNORECASE)
    visited: set[str] = set()
    page_urls: set[tuple[str, str | None]] = set()

    async def recurse(current_url: str, depth: int) -> None:
        if depth > max_depth or current_url in visited or len(page_urls) >= max_urls:
            return
        visited.add(current_url)
        text = await _fetch_and_decode(fetcher, current_url, {}, timeout_seconds)
        if not text:
            return
        locs = loc_pattern.findall(text)
        if "<sitemapindex" in text.lower():
            # Fan out: fetch all child sitemaps of an index concurrently.
            children = [c for c in locs if c not in visited]
            pending = [asyncio.create_task(recurse(child, depth + 1)) for child in children]
            if pending:
                # Bounded fan-out: stragglers are cancelled so one slow server
                # cannot gate the entire resolution.
                done, _ = await asyncio.wait(pending, timeout=45)
                for t in pending:
                    if t not in done:
                        t.cancel()
            return
        # Page sitemap: associate each <loc> with its neighboring <lastmod>.
        # Use byte offsets so this works whether entries are wrapped in <url>
        # blocks or listed as bare <loc> elements (some CMS emit the latter).
        loc_items = [(m.start(), m.group(1)) for m in loc_pattern.finditer(text)]
        lastmod_items = [(m.start(), m.group(1).strip()) for m in lastmod_re.finditer(text)]
        for idx, (pos, loc) in enumerate(loc_items):
            next_pos = loc_items[idx + 1][0] if idx + 1 < len(loc_items) else len(text)
            lastmod = next((val for lpos, val in lastmod_items if pos < lpos < next_pos), None)
            page_urls.add((loc, lastmod))

    cached = _SITEMAP_CACHE.get(sitemap_url)
    now = time.monotonic()
    if cached and now - cached[0] < _SITEMAP_CACHE_TTL:
        return set(cached[1])
    try:
        await asyncio.gather(*[recurse(sitemap_url, 0)])
    except Exception:
        pass
    _SITEMAP_CACHE[sitemap_url] = (now, list(page_urls))
    if len(_SITEMAP_CACHE) > 1000:
        oldest = sorted(_SITEMAP_CACHE.items(), key=lambda x: x[1][0])[:200]
        for k, _v in oldest:
            _SITEMAP_CACHE.pop(k, None)
    return page_urls
