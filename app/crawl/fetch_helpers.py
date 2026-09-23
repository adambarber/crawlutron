"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar
from urllib.parse import urljoin

from app.bounded_ttl import BoundedTTLCache
from app.constants import (
    DISCOVERY_BODY_MAX_BYTES,
    SITEMAP_CACHE_MAX_ENTRIES,
    SITEMAP_CACHE_TTL_SECONDS,
    SITEMAP_FANOUT_TIMEOUT_SECONDS,
    SITEMAP_INDEX_FANOUT,
    SITEMAP_MAX_DEPTH,
    SITEMAP_MAX_URLS,
)
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.strategies.body_decode import _decode_body
from app.validation import HostPolicy, safe_public_url_or_none

_T = TypeVar("_T")
_K = TypeVar("_K")


# Discovery bodies (robots.txt / sitemap.xml) are attacker-influenced; cap their
# decompressed size so a hostile origin cannot OOM the service via a gzip bomb.


async def _fetch_decode_discovery(
    fetcher: Any,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> tuple[str | None, int | None, dict[str, str] | None]:
    """Fetch, decompress, and decode a discovery body (robots.txt / sitemap.xml).

    The single place the discovery fetch policy lives: sync or coroutine
    fetcher, lowercase response headers, and a capped decompress so a hostile
    origin cannot OOM the service with a gzip bomb
    (``DISCOVERY_BODY_MAX_BYTES``). Returns ``(text, status, headers)``; the
    header map is present even when the body is absent -- non-200, or a body
    that blew the cap -- so the sitemap path can still read ETag/Last-Modified
    while the robots path ignores it. ``(None, None, None)`` on
    transport-level failure.
    """
    from app.crawl.decompression import DecompressionTooLargeError

    try:
        resp = fetcher(url, headers, timeout_seconds)
        if inspect.iscoroutine(resp):
            resp = await resp
        status = resp.status_code
        resp_headers = {key.lower(): value for key, value in resp.headers.items()}
        if status == 200:
            try:
                body = _decompress_body(
                    resp.body,
                    resp_headers.get("content-encoding", ""),
                    max_output_bytes=DISCOVERY_BODY_MAX_BYTES,
                )
            except DecompressionTooLargeError:
                return None, status, resp_headers
            return _decode_body(body, resp_headers.get("content-type")), status, resp_headers
        return None, status, resp_headers
    except Exception:
        pass
    return None, None, None


async def _fetch_and_decode(
    fetcher: Any,
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
) -> tuple[str | None, int | None]:
    """Fetch a URL, returning ``(decoded body, HTTP status)``.

    The body is present only on 200; the status lets callers distinguish
    "no such document" (404) from "origin is erroring" (5xx), which
    robots handling treats differently (fail closed, M7). ``(None, None)``
    on transport-level failure.
    """
    text, status, _ = await _fetch_decode_discovery(fetcher, url, headers, timeout_seconds)
    return text, status


async def _single_flight(
    inflight: dict[_K, asyncio.Task[_T]],
    key: _K,
    factory: Callable[[], Awaitable[_T]],
) -> _T:
    task = inflight.get(key)
    if task is None:

        async def run() -> _T:
            return await factory()

        task = asyncio.create_task(run())
        inflight[key] = task

        def discard(done: asyncio.Task[_T]) -> None:
            if inflight.get(key) is done:
                inflight.pop(key, None)

        task.add_done_callback(discard)
    return await asyncio.shield(task)


async def _fetch_text(
    fetcher: Any,
    url: str,
    cond_headers: dict[str, str] | None,
    timeout_seconds: int,
) -> tuple[str | None, str | None, str | None, bool]:
    """Fetch + decode a sitemap body, returning ``(text, etag, last_modified, not_modified)``.

    The sitemap layer's only addition over the shared core is conditional
    semantics: a 304 means the body is unchanged, so callers reuse a
    previously resolved set instead of re-parsing. ETag/Last-Modified are
    surfaced even when the body is absent (304, or a body that blew the
    decompression cap) so the cache entry stays addressable.
    """
    text, status, resp_headers = await _fetch_decode_discovery(
        fetcher, url, cond_headers or {}, timeout_seconds
    )
    if resp_headers is None or status not in (200, 304):
        return None, None, None, False
    etag = resp_headers.get("etag")
    last_modified = resp_headers.get("last-modified")
    if status == 304:
        return None, etag, last_modified, True
    return text, etag, last_modified, False


# Sitemap cache: one locked, bounded, TTL'd store (app.bounded_ttl).
#
# The lock lives inside the chokepoint, so call sites can never forget it
# (the class of bug that produced the robots-cache and DNS-cache races).
_SITEMAP_CACHE: BoundedTTLCache[tuple[str, HostPolicy], tuple[list, str | None, str | None]] = BoundedTTLCache(
    max_entries=SITEMAP_CACHE_MAX_ENTRIES, ttl=SITEMAP_CACHE_TTL_SECONDS
)


async def fetch_sitemap_urls(
    fetcher: Any,
    sitemap_url: str,
    timeout_seconds: int,
    *,
    max_depth: int = SITEMAP_MAX_DEPTH,
    max_urls: int = SITEMAP_MAX_URLS,
    conditional: dict[str, str] | None = None,
    host_policy: HostPolicy,
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

    loc_pattern = _re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", _re.IGNORECASE)
    lastmod_re = _re.compile(r"<lastmod>\s*([^<]+)\s*</lastmod>", _re.IGNORECASE)
    visited: set[str] = set()
    page_urls: set[tuple[str, str | None]] = set()

    async def recurse(current_url: str, depth: int, cond_headers: dict[str, str] | None = None):
        normalized_url = safe_public_url_or_none(current_url, policy=host_policy)
        if normalized_url is None:
            return None, None
        current_url = str(normalized_url)
        etag: str | None = None
        last_modified: str | None = None
        if depth > max_depth or current_url in visited or len(page_urls) >= max_urls:
            return etag, last_modified
        visited.add(current_url)
        text, etag, last_modified, not_modified = await _fetch_text(
            fetcher, current_url, cond_headers, timeout_seconds
        )
        if not_modified:
            # 304: reuse the previously resolved URLs for this exact URL.
            # get_stale (not get): the entry must be PRESENT, not fresh --
            # the origin just confirmed the last-known set is still valid.
            prev = _SITEMAP_CACHE.get_stale((current_url, host_policy))
            if prev:
                for entry in prev[0]:
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
            if safe_public_url_or_none(urljoin(current_url, loc), policy=host_policy) is not None:
                page_urls.add((loc, lastmod))
        return etag, last_modified

    cache_key = (sitemap_url, host_policy)
    cached = _SITEMAP_CACHE.get(cache_key)
    if cached is not None:
        # Fresh: serve directly (get() already LRU-touched).
        return set(cached[0])
    # Expired (or absent): fall through and re-fetch. Leave the entry in the
    # cache so a 304 response in recurse() can still reuse its URLs (do NOT pop
    # it here, or the conditional 304 reuse below would lose the cached set).
    # On a cache expiry, replay the prior ETag/Last-Modified as a conditional
    # request unless the caller supplied their own headers.
    if conditional is None and cached:
        if cached[1]:
            conditional = {"If-None-Match": cached[1]}
        elif cached[2]:
            conditional = {"If-Modified-Since": cached[2]}
    try:
        results = await asyncio.gather(*[recurse(sitemap_url, 0, conditional)])
        root_etag, root_lm = results[0]
    except Exception:
        root_etag, root_lm = (cached[1] if cached else None), (cached[2] if cached else None)
    # Store through the chokepoint: LRU recency refresh and bounded eviction
    # happen inside put(), under its lock (no more manual popitem loop).
    _SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm))
    return page_urls
