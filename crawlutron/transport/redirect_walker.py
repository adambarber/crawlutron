"""RedirectWalker: the single redirect-walk implementation.

Owns the fetch walk: one transport.fetch() per hop, redirect following,
deferred cookie capture, cross-origin credential stripping, and pin
validation in one place. ``redirect_target`` (pinned_transport) is the one
redirect decision, and this walker is the only owner of hop-to-hop state, so
one chain always yields one verdict -- no second walk implementation coexists
with it.

``store_cookies`` is the sanctioned cookie tail (``fetchers.store_hop_cookies``,
injected so this module never imports ``fetchers``).
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import cast

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.bounded_io import BodyTooLarge
from crawlutron.safety.header_values import reject_injectable_header_values
from crawlutron.transport import pinned_dns as _dns
from crawlutron.transport.pinned_transport import (
    PinnedRequest,
    PinRule,
    build_pinned_request,
    cffi_resolve_entry,
    redirect_target,
)
from crawlutron.transport.transport import FetchRequest, Transport, http_413
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

StoreHopCookies = Callable[[NormalizedUrl, Mapping[str, str]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _HopFrame:
    """One hop's shared frame: parameters every arm forwards."""

    url: NormalizedUrl
    timeout_seconds: float
    max_bytes: int
    started: float


class RedirectWalker:
    """The single redirect-walk implementation (see module docstring)."""

    def __init__(
        self,
        transport: Transport,
        store_cookies: StoreHopCookies,
    ) -> None:
        self._transport = transport
        self._store_cookies = store_cookies

    async def walk(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        timeout_seconds: float,
        *,
        max_bytes: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
        pin: PinRule | None = None,
        started: float | None = None,
    ) -> HttpFetchResult | None:
        """Walk one fetch from ``url`` following redirects to a terminal response.

        Returns the terminal ``HttpFetchResult`` or propagates; cffi transport
        failure raises and the None/fallback is handled in fetchers, not here.
        """
        if started is None:
            started = time.monotonic()
        # One walk object: hop state lives in locals; one verdict per chain.
        current_url = url
        current_headers: Mapping[str, str] | dict[str, str] = dict(headers)
        hops = 0
        rule = pin or build_pinned_request
        hop = _HopFrame(url=url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, started=started)
        pending: tuple[NormalizedUrl, Mapping[str, str]] | None = None
        # The shared wire edge: a header value that could end one header and
        # start another is refused before the first hop.
        reject_injectable_header_values(headers)

        while True:
            pinned = await self._admit_hop(current_url, current_headers, rule, timeout_seconds)

            # The previous hop's Set-Cookie flushes here: reaching this point
            # means this hop's pin admitted the walk.
            await _flush_deferred_hop_cookies(pending, self._store_cookies)
            pending = None
            request = self._build_request(pinned, timeout_seconds)

            try:
                result = await self._transport.fetch(request, max_bytes=max_bytes)
            except BodyTooLarge:
                # Multi-hop cap breach: url is the original admitted URL
                # (hop.url is never reassigned on redirect), final_url is the
                # terminal-hop logical URL (current_url advanced per hop).
                return http_413(hop.url, current_url, hop.started)

            next_hop, cookie_headers = await self._handle_hop_result(
                result, current_url, current_headers, hops
            )
            if next_hop is None:
                # Terminal: persist this hop's cookies and return the result.
                await self._store_cookies(current_url, cookie_headers)
                return result

            # Redirect: defer this hop's cookies until the next pin admits.
            # RFC 6265 5.3: the Set-Cookie belongs to the origin that sent it
            # (current_url before advancing), not the redirect target.
            pending = (current_url, cookie_headers)
            current_url, current_headers = next_hop
            hops += 1

    async def _admit_hop(
        self,
        url: NormalizedUrl,
        headers: Mapping[str, str] | dict[str, str],
        rule: PinRule,
        timeout_seconds: float,
    ) -> PinnedRequest:
        """The pin admits this hop FIRST: a refused pin means the previous hop's
        Set-Cookie is never planted (no state leaks to an unreached target).
        Service-built additions (Host, hop cookies) ride the same wire-edge law."""
        pinned = await _dns.bounded_thread_resolve(
            cast(
                Callable[[], PinnedRequest],
                lambda _url=url, _headers=headers: rule(_url, dict(_headers)),
            ),
            timeout_seconds=timeout_seconds,
        )
        reject_injectable_header_values(pinned.headers)
        return pinned

    async def _handle_hop_result(
        self,
        result: HttpFetchResult,
        current_url: NormalizedUrl,
        current_headers: Mapping[str, str] | dict[str, str],
        hops: int,
    ) -> tuple[tuple[NormalizedUrl, Mapping[str, str]] | None, Mapping[str, str] | dict[str, str]]:
        """Decide redirect-vs-terminal for one hop result: the next hop and its
        deferred cookie headers, or None when the response is terminal."""
        target = await self._advance(current_url, dict(current_headers), hops, result)
        cookie_headers = result._raw_headers if result._raw_headers is not None else result.headers
        return target, cookie_headers

    def _build_request(self, pinned: PinnedRequest, timeout_seconds: float) -> FetchRequest:
        """Build one hop's fetch request from the pinned request."""
        resolve_entry = cffi_resolve_entry(pinned)
        return FetchRequest(
            connect_url=pinned.connect_url,
            headers=pinned.headers,
            url=pinned.url,
            timeout_seconds=timeout_seconds,
            resolve_entry=resolve_entry,
        )

    async def _advance(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        hops: int,
        result: HttpFetchResult,
    ) -> tuple[NormalizedUrl, dict[str, str]] | None:
        """The single redirect decision: ``redirect_target`` applied to hop state.

        Returns the next hop's ``(url, headers)``, or None when the response is
        terminal. A second walk implementation used to decide this -- deleted,
        so one chain yields one verdict.
        """
        return await redirect_target(url, headers, result.status_code, result.headers.get("location"), hops)


async def _flush_deferred_hop_cookies(
    deferred: tuple[NormalizedUrl, Mapping[str, str]] | None,
    store_cookies: StoreHopCookies,
) -> None:
    """Store the previous hop's Set-Cookie exactly once: reaching a fresh loop
    iteration means this hop's pin admitted the walk."""
    if deferred is not None:
        await store_cookies(*deferred)
