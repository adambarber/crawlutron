"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, NamedTuple

from app.bounded_ttl import BoundedTTLCache
from app.models.host_policy import HostPolicy
from app.models.http_fetch_result import HttpFetchResult
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.transport.body_decode import decode_body
from app.transport.decompression import decompress_body as _decompress_body
from app.vocabulary.limits import (
    DISCOVERY_BODY_MAX_BYTES,
    SITEMAP_CACHE_MAX_ENTRIES,
    SITEMAP_CACHE_TTL_SECONDS,
    SITEMAP_FANOUT_TIMEOUT_SECONDS,
    SITEMAP_INDEX_FANOUT,
    SITEMAP_MAX_DEPTH,
    SITEMAP_MAX_URLS,
)

# Request headers as the discovery fetch takes them: a mapping it only ever
# reads, or None for "send none". One declaration, because the fetch signatures
# and the fetcher contract both spell it -- while they each spelled
# ``dict[str, str]`` on their own, the sitemap path had to write
# ``cond_headers or {}`` at its call site to satisfy the core, which is the rule
# the core now owns.
type _RequestHeaders = Mapping[str, str] | None


# Discovery bodies (robots.txt / sitemap.xml) are attacker-influenced; cap their
# decompressed size so a hostile origin cannot OOM the service via a gzip bomb.


class _Discovery(NamedTuple):
    """One discovery response (robots.txt / sitemap.xml): the owner of what a
    response *means*.

    ``headers is None`` is the fetch core's transport-failure marker. Every
    accessor below is total over it, so a consumer cannot reach a ``.get`` on a
    missing header map -- the failure the sitemap path previously had to guard
    by hand with ``resp_headers is None or status not in (200, 304)`` and then
    trust for the two unguarded ``.get`` calls on the following lines.
    """

    text: str | None
    status: int | None
    headers: dict[str, str] | None

    def validators(self) -> tuple[str | None, str | None] | None:
        """``(etag, last_modified)`` when this response can revalidate a cache.

        ``None`` when it cannot: a transport failure carries no headers, and a
        status outside 200/304 revalidates nothing. Those are one decision
        because they must agree -- as two separate predicates, a caller can
        check one and then read through the other, which is the shape this
        removed.
        """
        if self.headers is None or self.status not in (200, 304):
            return None
        return self.headers.get("etag"), self.headers.get("last-modified")

    def not_modified(self) -> bool:
        """True only when the origin confirmed the cached body is unchanged."""
        return self.status == 304


async def _call_fetcher(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> HttpFetchResult:
    """Invoke the fetcher, tolerating the sync fetcher doubles the suite passes.

    The production protocol is async, so the honest type of a conforming
    fetcher's return is the coroutine; a sync double returns the result
    directly, which is not that type, and the one ``Any`` below is where the
    seam's tolerance for it lives -- stated once, here, instead of as an
    untyped ``fetcher`` parameter at every call site the seam used to have.
    When the doubles go async-native, this helper is the single deletion
    point (along with the strategy seam's inline copy in fetch_policy).
    """
    # ``_RequestHeaders`` promises a mapping the fetch only reads; the
    # protocol promises a dict. The copy is the whole bridge between the two.
    sent = {} if headers is None else dict(headers)
    resp: Any = fetcher(url, sent, timeout_seconds)
    if inspect.iscoroutine(resp):
        resp = await resp
    return resp


async def _fetch_decode_discovery(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> _Discovery:
    """Fetch, decompress, and decode a discovery body (robots.txt / sitemap.xml).

    The single place the discovery fetch policy lives: sync or coroutine
    fetcher, lowercase response headers, and a capped decompress so a hostile
    origin cannot OOM the service with a gzip bomb
    (``DISCOVERY_BODY_MAX_BYTES``). Absent ``headers`` means "send none", so a
    caller passes its conditional headers straight through instead of
    normalizing them first. The header map is present even when the body is
    absent -- non-200, or a body that blew the cap -- so the sitemap path can
    still read ETag/Last-Modified while the robots path ignores it. All three
    fields are ``None`` on transport-level failure, and ``_Discovery`` is the
    only thing a consumer is meant to read off the result.
    """
    from app.transport.decompression import DecompressionTooLargeError

    with contextlib.suppress(Exception):
        resp = await _call_fetcher(fetcher, url, headers or {}, timeout_seconds)
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
                return _Discovery(None, status, resp_headers)
            return _Discovery(decode_body(body, resp_headers.get("content-type")), status, resp_headers)
        return _Discovery(None, status, resp_headers)
    return _Discovery(None, None, None)


async def fetch_and_decode(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> tuple[str | None, int | None]:
    """Fetch a URL, returning ``(decoded body, HTTP status)``.

    The body is present only on 200; the status lets callers distinguish
    "no such document" (404) from "origin is erroring" (5xx), which
    robots handling treats differently (fail closed). ``(None, None)``
    on transport-level failure.
    """
    resp = await _fetch_decode_discovery(fetcher, url, headers, timeout_seconds)
    return resp.text, resp.status


async def _single_flight[K, T](
    inflight: dict[K, asyncio.Task[T]],
    key: K,
    factory: Callable[[], Awaitable[T]],
) -> T:
    task = inflight.get(key)
    if task is None:

        async def run() -> T:
            return await factory()

        task = asyncio.create_task(run())
        inflight[key] = task

        def discard(done: asyncio.Task[T]) -> None:
            if inflight.get(key) is done:
                inflight.pop(key, None)

        task.add_done_callback(discard)
    return await asyncio.shield(task)


async def _fetch_text(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
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
    resp = await _fetch_decode_discovery(fetcher, url, cond_headers, timeout_seconds)
    validators = resp.validators()
    if validators is None:
        return None, None, None, False
    etag, last_modified = validators
    if resp.not_modified():
        return None, etag, last_modified, True
    return resp.text, etag, last_modified, False


# Sitemap cache: one locked, bounded, TTL'd store (app.bounded_ttl).
#
# The lock lives inside the chokepoint, so call sites can never forget it
# (the class of bug that produced the robots-cache and DNS-cache races).
_SITEMAP_CACHE: BoundedTTLCache[tuple[str, HostPolicy], tuple[list, str | None, str | None]] = (
    BoundedTTLCache(max_entries=SITEMAP_CACHE_MAX_ENTRIES, ttl=SITEMAP_CACHE_TTL_SECONDS)
)


async def fetch_sitemap_urls(
    fetcher: HttpFetcher,
    sitemap_url: NormalizedUrl,
    timeout_seconds: int,
    *,
    max_depth: int = SITEMAP_MAX_DEPTH,
    max_urls: int = SITEMAP_MAX_URLS,
    conditional: dict[str, str] | None = None,
    host_policy: HostPolicy,
) -> set[tuple[NormalizedUrl, str | None]]:
    """Recursively resolve a sitemap URL into ``(loc, lastmod)`` pairs.

    Handles sitemap indexes (``<sitemapindex>`` pointing at child sitemaps) up
    to ``max_depth`` levels, and caps total collected URLs at ``max_urls`` so a
    hostile or enormous index cannot run away.

    Each collected entry is a ``(url, lastmod)`` tuple where ``url`` is the
    admitted ``NormalizedUrl`` -- resolved here, at the one place that knows
    the sitemap they came from, so no caller re-joins or re-admits -- and
    ``lastmod`` is the sitemap ``<lastmod>`` for that URL when present (used
    to skip re-fetching unchanged pages); it is ``None`` when the entry has
    no ``<lastmod>``.

    If a previous resolution cached an ETag/Last-Modified (or the caller passes
    ``conditional``), the root sitemap is fetched with a conditional request
    A 304 reuses the cached URLs without re-parsing the body, which
    avoids re-downloading unchanged sitemaps on every crawl.
    """
    import re as _re

    loc_pattern = _re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", _re.IGNORECASE)
    lastmod_re = _re.compile(r"<lastmod>\s*([^<]+)\s*</lastmod>", _re.IGNORECASE)
    visited: set[NormalizedUrl] = set()
    page_urls: set[tuple[NormalizedUrl, str | None]] = set()

    async def recurse(current_url: NormalizedUrl, depth: int, cond_headers: dict[str, str] | None = None):
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
            prev = _SITEMAP_CACHE.get_stale((current_url.raw, host_policy))
            if prev:
                page_urls.update(prev[0])
            return etag, last_modified
        if text is None:
            return etag, last_modified
        locs = loc_pattern.findall(text)
        if "<sitemapindex" in text.lower():
            # Bounded fan-out: never schedule more children than still
            # needed to reach max_urls, and fetch them in capped chunks so a
            # hostile index with tens of thousands of children cannot spawn
            # a task (and a fetch) per child.
            children = locs
            children = children[: max(0, max_urls - len(page_urls))]
            for offset in range(0, len(children), SITEMAP_INDEX_FANOUT):
                if len(page_urls) >= max_urls:
                    break
                chunk = children[offset : offset + SITEMAP_INDEX_FANOUT]
                pending = []
                for child in chunk:
                    admitted = current_url.resolve(child)
                    if admitted is not None and admitted not in visited:
                        pending.append(asyncio.create_task(recurse(admitted, depth + 1)))
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
            admitted = current_url.resolve(loc)
            if admitted is not None:
                page_urls.add((admitted, lastmod))
        return etag, last_modified

    cache_key = (sitemap_url.raw, host_policy)
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
        root_etag, root_lm = await recurse(sitemap_url, 0, conditional)
    except Exception:
        root_etag, root_lm = (cached[1] if cached else None), (cached[2] if cached else None)
    # Store through the chokepoint: LRU recency refresh and bounded eviction
    # happen inside put(), under its lock (no more manual popitem loop).
    _SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm))
    return page_urls


def reset_process_state() -> None:
    """Drop cached sitemap documents.

    The cache is process-wide and its TTL is hours, so a test that crawls a sitemap
    once sees the same document for the rest of the session unless it clears this.
    """
    _SITEMAP_CACHE.clear()


def has_process_state() -> bool:
    """Whether any sitemap document is cached right now."""
    return bool(_SITEMAP_CACHE)
