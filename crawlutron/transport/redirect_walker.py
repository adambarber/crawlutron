"""RedirectWalker: owns the fetch walk (redirects, cookie capture, pin validation,
header checks), calling ``transport.fetch()`` once per hop.

Capping happens via :func:`crawlutron.safety.bounded_io.read_capped_async` -- one
function, both transports. Cookie capture lives in the walker, not per-backend,
which makes the C1 cookie-injection bug structurally impossible.

The cookie-store callable is injected (never imported here from ``fetchers``),
so this module has no import edge back to the fetch layer -- ``fetchers`` owns
both the walker and the sanctioned cookie tail, one direction only.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.bounded_io import BodyTooLarge
from crawlutron.safety.header_values import reject_injectable_header_values
from crawlutron.transport.pinned_transport import (
    PinnedRequest,
    PinRule,
    RedirectChain,
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
    timeout_seconds: int
    max_bytes: int
    started: float


class RedirectWalker:
    """Owns the redirect walk: one transport.fetch() per hop, with redirect
    following, deferred cookie capture, cross-origin credential stripping, and
    pin validation all in one place.

    ``store_cookies`` is the sanctioned cookie tail (``fetchers.store_hop_cookies``,
    injected so this module never imports ``fetchers``).
    """

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
        timeout_seconds: int,
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
        chain = RedirectChain(url, headers, pin=pin or build_pinned_request, timeout_seconds=timeout_seconds)
        pending: tuple[NormalizedUrl, Mapping[str, str]] | None = None
        hop = _HopFrame(url=url, timeout_seconds=timeout_seconds, max_bytes=max_bytes, started=started)

        # The shared wire edge: a header value that could end one header and
        # start another is refused before the first hop.
        reject_injectable_header_values(headers)

        while True:
            # The pin admits this hop FIRST: a refused pin means the previous
            # hop's Set-Cookie is never planted (no state leaks to an
            # unreached target).
            pinned = await chain.pin()
            # Service-built additions (Host, hop cookies) ride the same law.
            reject_injectable_header_values(pinned.headers)

            # The previous hop's Set-Cookie flushes here: reaching this point
            # means this hop's pin admitted the walk.
            await _flush_deferred_hop_cookies(pending, self._store_cookies)
            pending = None

            request = self._build_request(chain, pinned, timeout_seconds)

            try:
                result = await self._transport.fetch(request, max_bytes=max_bytes)
            except BodyTooLarge:
                # Multi-hop cap breach: url is the original admitted URL
                # (hop.url is never reassigned on redirect), final_url is the
                # terminal-hop logical URL (chain.url advanced per hop).
                return http_413(hop.url, chain.url, hop.started)

            outcome, pending = await self._process_result(result, chain, started)
            if outcome is not None:
                return outcome

    def _build_request(
        self, chain: RedirectChain, pinned: PinnedRequest, timeout_seconds: int
    ) -> FetchRequest:
        """Build one hop's fetch request from the chain and pinned request."""
        resolve_entry = cffi_resolve_entry(pinned)
        return FetchRequest(
            connect_url=pinned.connect_url,
            headers=pinned.headers,
            url=pinned.url,
            timeout_seconds=timeout_seconds,
            resolve_entry=resolve_entry,
        )

    async def _process_result(
        self,
        result: HttpFetchResult,
        chain: RedirectChain,
        started: float,
    ) -> tuple[HttpFetchResult | None, tuple[NormalizedUrl, Mapping[str, str]] | None]:
        """Decide whether a hop result is terminal or a redirect; update chain
        state on redirect and defer this hop's cookies until the next pin admits."""
        target = await redirect_target(
            chain.url,
            chain.headers,
            result.status_code,
            result.headers.get("location"),
            chain.hops,
        )
        if target is None:
            # Terminal: persist this hop's cookies and return the result.
            cookie_headers = result._raw_headers if result._raw_headers is not None else result.headers
            await self._store_cookies(chain.url, cookie_headers)
            return result, None

        # Redirect: defer this hop's cookies until the next pin admits the walk.
        # RFC 6265 5.3: the Set-Cookie belongs to the origin that sent it
        # (chain.url before advancing), not the redirect target.
        cookie_headers = result._raw_headers if result._raw_headers is not None else result.headers
        pending = (chain.url, cookie_headers)
        chain.url, chain.headers = target
        chain.hops += 1
        return None, pending


async def _flush_deferred_hop_cookies(
    deferred: tuple[NormalizedUrl, Mapping[str, str]] | None,
    store_cookies: StoreHopCookies,
) -> None:
    """Store the previous hop's Set-Cookie exactly once: reaching a fresh loop
    iteration means this hop's pin admitted the walk."""
    if deferred is not None:
        await store_cookies(*deferred)
