"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from collections import OrderedDict
from typing import Any

from app.constants import (
    DISCOVERY_BODY_MAX_BYTES,
    SITEMAP_CACHE_EVICT_BATCH,
    SITEMAP_CACHE_MAX_ENTRIES,
    SITEMAP_CACHE_TTL_SECONDS,
    SITEMAP_FANOUT_TIMEOUT_SECONDS,
    SITEMAP_INDEX_FANOUT,
    SITEMAP_MAX_DEPTH,
    SITEMAP_MAX_URLS,
)
from app.crawl.strategies.http import _decode_body, _decompress_body


# Discovery bodies (robots.txt / sitemap.xml) are attacker-influenced; cap their
# decompressed size so a hostile origin cannot OOM the service via a gzip bomb.


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
                    max_output_bytes=DISCOVERY_BODY_MAX_BYTES,
                )
            except DecompressionTooLargeError:
                return None
            return _decode_body(body, resp_headers.get("content-type"))
    except Exception:  # noqa: BLE001
        pass
    return None


# Sitemap cache (LRU via OrderedDict.move_to_end + popitem).
#
# Thread-safety: under CPython this is safe in practice because the GIL
# serializes Python bytecode for OrderedDict mutations. The cache is only
# read/written from the asyncio event loop (no threads), so even a GIL-free
# runtime (PyPy, GraalPy) would see single-threaded access. If a future
# change moves sitemap parsing to ``asyncio.to_thread``, this cache will
# need an explicit ``threading.Lock`` (mirroring ``_DNS_CACHE_LOCK`` in
# ``app/crawl/strategies/http.py``).
_SITEMAP_CACHE: "OrderedDict[str, tuple[float, list, str | None, str | None]]" = OrderedDict()


async def fetch_sitemap_urls(
    fetcher: Any,
    sitemap_url: str,
    timeout_seconds: int,
    *,
    max_depth: int = SITEMAP_MAX_DEPTH,
    max_urls: int = SITEMAP_MAX_URLS,
    conditional: dict[str, str] | None = None,
) -> set[tuple[str, str | None]]:
    """Recursively resolve a sitemap URL into ``(loc, lastmod)`` pairs.

    Handles sitemap indexes (``<sitemapindex>`` pointing at child sitemaps) up
    to ``max_depth`` levels, and caps total collected URLs at ``max_urls`` so a
    hostile or enormous index cannot run away.

    Each collected entry is a ``(url, lastmod)`` tuple where ``lastmod`` is the
    sitemap ``<lastmod>`` for that URL when present (used to skip re-fetching
    unchanged pages, #1); it is ``None`` when the entry has no ``<lastmod>``.

    If a previous resolution cached an ETag/Last-Modified (or the caller passes
    ``conditional``), the root sitemap is fetched with a conditional request
    (#5). A 304 reuses the cached URLs without re-parsing the body, which
    avoids re-downloading unchanged sitemaps on every crawl.
    """
    import re as _re
    from app.crawl.decompression import DecompressionTooLargeError

    loc_pattern = _re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", _re.IGNORECASE)
    lastmod_re = _re.compile(r"<lastmod>\s*([^<]+)\s*</lastmod>", _re.IGNORECASE)
    visited: set[str] = set()
    page_urls: set[tuple[str, str | None]] = set()

    async def _fetch_text(url: str, cond_headers: dict[str, str] | None):
        """Fetch + decode a sitemap body, returning (text, etag, last_modified, not_modified)."""
        try:
            resp = fetcher(url, cond_headers or {}, timeout_seconds)
            if inspect.iscoroutine(resp):
                resp = await resp
            rh = {key.lower(): value for key, value in resp.headers.items()}
            if resp.status_code == 304:
                return None, rh.get("etag"), rh.get("last-modified"), True
            if resp.status_code == 200:
                try:
                    body = _decompress_body(
                        resp.body,
                        rh.get("content-encoding", ""),
                        max_output_bytes=DISCOVERY_BODY_MAX_BYTES,
                    )
                except DecompressionTooLargeError:
                    return None, rh.get("etag"), rh.get("last-modified"), False
                return _decode_body(body, rh.get("content-type")), rh.get("etag"), rh.get("last-modified"), False
        except Exception:  # noqa: BLE001
            pass
        return None, None, None, False

    async def recurse(current_url: str, depth: int, cond_headers: dict[str, str] | None = None):
        etag: str | None = None
        last_modified: str | None = None
        if depth > max_depth or current_url in visited or len(page_urls) >= max_urls:
            return etag, last_modified
        visited.add(current_url)
        text, etag, last_modified, not_modified = await _fetch_text(current_url, cond_headers)
        if not_modified:
            # 304: reuse the previously resolved URLs for this exact URL.
            prev = _SITEMAP_CACHE.get(current_url)
            if prev:
                _SITEMAP_CACHE.move_to_end(current_url)
                for entry in prev[1]:
                    page_urls.add(entry)
            return etag, last_modified
        if text is None:
            return etag, last_modified
        locs = loc_pattern.findall(text)
        if "<sitemapindex" in text.lower():
            # Bounded fan-out (H7): never schedule more children than still
            # needed to reach max_urls, and fetch them in capped chunks so a
            # hostile index with tens of thousands of children cannot spawn
            # a task (and a fetch) per child.
            children = [c for c in locs if c not in visited]
            children = children[: max(0, max_urls - len(page_urls))]
            for offset in range(0, len(children), SITEMAP_INDEX_FANOUT):
                if len(page_urls) >= max_urls:
                    break
                chunk = children[offset : offset + SITEMAP_INDEX_FANOUT]
                pending = [asyncio.create_task(recurse(child, depth + 1)) for child in chunk]
                if pending:
                    # Stragglers are cancelled so one slow server cannot gate
                    # the entire resolution.
                    done, _ = await asyncio.wait(pending, timeout=SITEMAP_FANOUT_TIMEOUT_SECONDS)
                    for t in pending:
                        if t not in done:
                            t.cancel()
            return etag, last_modified
        # Page sitemap: associate each <loc> with its neighboring <lastmod>.
        # Use byte offsets so this works whether entries are wrapped in <url>
        # blocks or listed as bare <loc> elements (some CMS emit the latter).
        loc_items = [(m.start(), m.group(1)) for m in loc_pattern.finditer(text)]
        lastmod_items = [(m.start(), m.group(1).strip()) for m in lastmod_re.finditer(text)]
        for idx, (pos, loc) in enumerate(loc_items):
            next_pos = loc_items[idx + 1][0] if idx + 1 < len(loc_items) else len(text)
            lastmod = next((val for lpos, val in lastmod_items if pos < lpos < next_pos), None)
            page_urls.add((loc, lastmod))
        return etag, last_modified

    cached = _SITEMAP_CACHE.get(sitemap_url)
    now = time.monotonic()
    if cached is not None and now - cached[0] < SITEMAP_CACHE_TTL_SECONDS:
        # Fresh: serve directly and mark recently used (LRU).
        _SITEMAP_CACHE.move_to_end(sitemap_url)
        return set(cached[1])
    # Expired (or absent): fall through and re-fetch. Leave the entry in the
    # cache so a 304 response in recurse() can still reuse its URLs (do NOT pop
    # it here, or the conditional 304 reuse below would lose the cached set).
    # On a cache expiry, replay the prior ETag/Last-Modified as a conditional
    # request unless the caller supplied their own headers.
    if conditional is None and cached:
        if cached[2]:
            conditional = {"If-None-Match": cached[2]}
        elif cached[3]:
            conditional = {"If-Modified-Since": cached[3]}
    try:
        results = await asyncio.gather(*[recurse(sitemap_url, 0, conditional)])
        root_etag, root_lm = results[0]
    except Exception:  # noqa: BLE001
        root_etag, root_lm = (cached[2] if cached else None), (cached[3] if cached else None)
    # Store with LRU recency and evict the least-recently-used entries
    # past the cap. OrderedDict gives O(1) move-to-end on access and
    # O(overflow) eviction without the previous full sort of the whole
    # cache on every overflow (B4/B5).
    _SITEMAP_CACHE[sitemap_url] = (now, list(page_urls), root_etag, root_lm)
    while len(_SITEMAP_CACHE) > SITEMAP_CACHE_MAX_ENTRIES:
        _SITEMAP_CACHE.popitem(last=False)
    return page_urls
