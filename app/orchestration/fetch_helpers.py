"""Shared HTTP fetch helpers for robots.txt and sitemap parsing.

Centralizes the common pattern of fetching, decompressing, and decoding
robots.txt/sitemap.xml responses used in engine.py.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import re as _re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from app.models.host_policy import HostPolicy
from app.models.http_fetch_result import HttpFetchResult
from app.models.http_fetcher import HttpFetcher
from app.models.normalized_url import NormalizedUrl
from app.safety.bounded_ttl import BoundedTTLCache
from app.transport.body_decode import decode_body
from app.transport.decompression import decompress_body as _decompress_body
from app.vocabulary.limits import (
    DISCOVERY_BODY_MAX_BYTES,
    SITEMAP_CACHE_MAX_ENTRIES,
    SITEMAP_CACHE_TTL_SECONDS,
    SITEMAP_FAILURE_TTL_SECONDS,
    SITEMAP_FANOUT_TIMEOUT_SECONDS,
    SITEMAP_INDEX_FANOUT,
    SITEMAP_MAX_DEPTH,
    SITEMAP_MAX_URLS,
)

# Request headers as the discovery fetch takes them: a mapping it only
# reads, or None for "send none" -- one declaration, owned by the core.
type _RequestHeaders = Mapping[str, str] | None


# Discovery bodies (robots.txt / sitemap.xml) are attacker-influenced; cap their
# decompressed size so a hostile origin cannot OOM the service via a gzip bomb.


class Discovery(NamedTuple):
    """One discovery response (robots.txt / sitemap.xml): the owner of what a
    response *means*. ``headers is None`` is the transport-failure marker, and
    every accessor is total over it, so a consumer never reaches a ``.get`` on a missing header map.
    """

    text: str | None
    status: int | None
    headers: dict[str, str] | None

    def validators(self) -> tuple[str | None, str | None] | None:
        """``(etag, last_modified)`` when this response can revalidate a cache; ``None``
        when it cannot (a transport failure carries no headers, a non-200/304
        revalidates nothing) -- one decision, because as two predicates a caller could read through a gap.
        """
        if self.headers is None or self.status not in (200, 304):
            return None
        return self.headers.get("etag"), self.headers.get("last-modified")

    def not_modified(self) -> bool:
        """True only when the origin confirmed the cached body is unchanged."""
        return self.status == 304


@dataclass
class _SitemapWalk:
    """The shared state of one recursion pass, mutated by every visited node.

    ``visited`` and ``page_urls`` are carried through the fan-out and mutated
    in place, so a walk that hits its caps resolves in fewer passes."""

    fetcher: HttpFetcher
    timeout_seconds: int
    host_policy: HostPolicy
    max_depth: int
    max_urls: int
    visited: set[NormalizedUrl]
    page_urls: set[tuple[NormalizedUrl, str | None]] = field(default_factory=set)


async def _call_fetcher(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> HttpFetchResult:
    """Invoke the fetcher, tolerating the sync fetcher doubles the suite passes.
    The production protocol is async (a coroutine return); a sync double returns
    the result directly, and the one ``Any`` admits the seam's tolerance here.
    """
    # ``_RequestHeaders`` promises a mapping the fetch only reads; the
    # protocol promises a dict. The copy is the whole bridge between the two.
    sent = {} if headers is None else dict(headers)
    resp: Any = fetcher(url, sent, timeout_seconds)
    if inspect.iscoroutine(resp):
        resp = await resp
    return resp


async def fetch_decode_discovery(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> Discovery:
    """Fetch, decompress and decode a discovery body (robots.txt / sitemap.xml):
    the single place the policy lives -- capped decompress against a gzip bomb.
    headers is None means "send none"; all fields are None on transport failure.
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
                return Discovery(None, status, resp_headers)
            return Discovery(decode_body(body, resp_headers.get("content-type")), status, resp_headers)
        return Discovery(None, status, resp_headers)
    return Discovery(None, None, None)


async def fetch_and_decode(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    headers: _RequestHeaders,
    timeout_seconds: int,
) -> tuple[str | None, int | None]:
    """Fetch a URL, returning ``(decoded body, HTTP status)``; the body is present
    only on 200, so the status lets callers tell a 404 from a 5xx (robots fails
    closed differently), and ``(None, None)`` is a transport-level failure.
    """
    resp = await fetch_decode_discovery(fetcher, url, headers, timeout_seconds)
    return resp.text, resp.status


async def single_flight[K, T](
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


async def fetch_text(
    fetcher: HttpFetcher,
    url: NormalizedUrl,
    cond_headers: dict[str, str] | None,
    timeout_seconds: int,
) -> tuple[str | None, str | None, str | None, bool]:
    """Fetch + decode a sitemap body: ``(text, etag, last_modified, not_modified)``.
    The sitemap layer's only addition is conditional semantics: a 304 means the
    body is unchanged, so callers reuse the previous set; validators stay addressable.
    """
    resp = await fetch_decode_discovery(fetcher, url, cond_headers, timeout_seconds)
    validators = resp.validators()
    if validators is None:
        return None, None, None, False
    etag, last_modified = validators
    if resp.not_modified():
        return None, etag, last_modified, True
    return resp.text, etag, last_modified, False


# Sitemap cache: one locked, bounded, TTL'd store (app.safety.bounded_ttl).
# The lock lives inside the chokepoint, so no call site can forget it.
SITEMAP_CACHE: BoundedTTLCache[tuple[str, HostPolicy], tuple[list, str | None, str | None]] = BoundedTTLCache(
    max_entries=SITEMAP_CACHE_MAX_ENTRIES, ttl=SITEMAP_CACHE_TTL_SECONDS
)


_LOC_PATTERN = _re.compile(r"<loc>\s*([^<\s]+)\s*</loc>", _re.IGNORECASE)
_LASTMOD_PATTERN = _re.compile(r"<lastmod>\s*([^<]+)\s*</lastmod>", _re.IGNORECASE)


def _collect_page_entries(
    text: str,
    current_url: NormalizedUrl,
    page_urls: set[tuple[NormalizedUrl, str | None]],
) -> None:
    """Associate each page ``<loc>`` with its neighboring ``<lastmod>``.

    Matched by byte offsets so wrapped and bare entry shapes both parse."""
    loc_items = [(m.start(), m.group(1)) for m in _LOC_PATTERN.finditer(text)]
    lastmod_items = [(m.start(), m.group(1).strip()) for m in _LASTMOD_PATTERN.finditer(text)]
    for idx, (pos, loc) in enumerate(loc_items):
        next_pos = loc_items[idx + 1][0] if idx + 1 < len(loc_items) else len(text)
        lastmod = next((val for lpos, val in lastmod_items if pos < lpos < next_pos), None)
        admitted = current_url.resolve(loc)
        if admitted is not None:
            page_urls.add((admitted, lastmod))


async def _fanout_sitemap_children(
    walk: _SitemapWalk, current_url: NormalizedUrl, children: list[str], depth: int
) -> None:
    """Schedule bounded fan-out over one sitemapindex's children."""
    for offset in range(0, len(children), SITEMAP_INDEX_FANOUT):
        if len(walk.page_urls) >= walk.max_urls:
            break
        chunk = children[offset : offset + SITEMAP_INDEX_FANOUT]
        pending = []
        for child in chunk:
            admitted = current_url.resolve(child)
            if admitted is not None and admitted not in walk.visited:
                pending.append(asyncio.create_task(_recurse_sitemap(walk, admitted, depth + 1, None)))
        if pending:
            # Stragglers are cancelled so one slow server cannot gate the
            # entire resolution.
            done, _ = await asyncio.wait(pending, timeout=SITEMAP_FANOUT_TIMEOUT_SECONDS)
            for t in pending:
                if t not in done:
                    t.cancel()


async def _recurse_sitemap(
    walk: _SitemapWalk,
    current_url: NormalizedUrl,
    depth: int,
    cond_headers: dict[str, str] | None,
) -> tuple[str | None, str | None]:
    """Resolve one sitemap node into ``walk.page_urls``, recursing into indexes.

    Mutates ``walk.page_urls``; returns ``(etag, last_modified)``."""
    etag: str | None = None
    last_modified: str | None = None
    if depth > walk.max_depth or current_url in walk.visited or len(walk.page_urls) >= walk.max_urls:
        return etag, last_modified
    walk.visited.add(current_url)
    text, etag, last_modified, not_modified = await fetch_text(
        walk.fetcher, current_url, cond_headers, walk.timeout_seconds
    )
    if not_modified:
        # 304: reuse the previously resolved URLs. get_stale, not get: the
        # entry must be PRESENT, not fresh -- the origin confirmed the set.
        prev = SITEMAP_CACHE.get_stale((current_url.raw, walk.host_policy))
        if prev:
            walk.page_urls.update(prev[0])
        return etag, last_modified
    if text is None:
        return etag, last_modified
    locs = _LOC_PATTERN.findall(text)
    if "<sitemapindex" in text.lower():
        children = locs[: max(0, walk.max_urls - len(walk.page_urls))]
        await _fanout_sitemap_children(walk, current_url, children, depth)
        return etag, last_modified
    _collect_page_entries(text, current_url, walk.page_urls)
    return etag, last_modified


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
    """Recursively resolve a sitemap URL into ``(loc, lastmod)`` pairs, following
    sitemap indexes up to ``max_depth`` levels and capping total collected URLs at
    ``max_urls`` so a hostile or enormous index cannot run away.
    """
    visited: set[NormalizedUrl] = set()
    page_urls: set[tuple[NormalizedUrl, str | None]] = set()
    walk = _SitemapWalk(
        fetcher=fetcher,
        timeout_seconds=timeout_seconds,
        host_policy=host_policy,
        max_depth=max_depth,
        max_urls=max_urls,
        visited=visited,
        page_urls=page_urls,
    )
    cache_key = (sitemap_url.raw, host_policy)
    fresh = SITEMAP_CACHE.get(cache_key)
    if fresh is not None:
        return set(fresh[0])
    stale = SITEMAP_CACHE.get_stale(cache_key)
    if conditional is None and stale:
        if stale[1]:
            conditional = {"If-None-Match": stale[1]}
        elif stale[2]:
            conditional = {"If-Modified-Since": stale[2]}
    try:
        root_etag, root_lm = await _recurse_sitemap(walk, sitemap_url, 0, conditional)
    except Exception:
        root_etag, root_lm = (stale[1] if stale else None), (stale[2] if stale else None)
    # A failure (walk raised, or nothing resolved with no validators) must not
    # stand for the full TTL: transient outages expire on the short clock.
    if not page_urls and root_etag is None and root_lm is None:
        SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm), ttl=SITEMAP_FAILURE_TTL_SECONDS)
    else:
        SITEMAP_CACHE.put(cache_key, (list(page_urls), root_etag, root_lm))
    return page_urls


def reset_process_state() -> None:
    """Drop cached sitemap documents.

    The cache is process-wide and its TTL is hours, so a test that crawls a sitemap
    once sees the same document for the rest of the session unless it clears this.
    """
    SITEMAP_CACHE.clear()


def has_process_state() -> bool:
    """Whether any sitemap document is cached right now."""
    return bool(SITEMAP_CACHE)
