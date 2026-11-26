"""RedirectWalker: the single redirect-walk implementation.

reads time.monotonic() for walk deadline

wall-consumer
"""

from __future__ import annotations

import math
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.safety.bounded_io import BodyTooLarge
from crawlutron.safety.header_values import reject_injectable_header_values
from crawlutron.transport.pinned_transport import redirect_target
from crawlutron.transport.transport import FetchRequest, Transport, http_408_deadline, http_413
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from safe_url.normalized_url import NormalizedUrl

StoreHopCookies = Callable[[NormalizedUrl, Mapping[str, str]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _HopFrame:
    """One hop's shared frame: the original admitted URL and the walk start."""

    url: NormalizedUrl
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
        started: float | None = None,
    ) -> HttpFetchResult | None:
        """Walk one fetch through redirects, returning the terminal result or raising.
        cffi transport failure raises; the proxy owns DNS and every hop stays logical.
        """
        if started is None:
            started = time.monotonic()
        if not math.isfinite(timeout_seconds) or timeout_seconds < 0:
            raise ValueError(
                f"walk timeout_seconds must be a finite non-negative number, got {timeout_seconds!r}"
            )
        hop = _HopFrame(url=url, started=started)
        reject_injectable_header_values(headers)
        deadline = started + timeout_seconds
        return await self._walk_loop(url, headers, hop, max_bytes, deadline)

    async def _walk_loop(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        hop: _HopFrame,
        max_bytes: int,
        deadline: float,
    ) -> HttpFetchResult | None:
        current_url = url
        current_headers: Mapping[str, str] | dict[str, str] = dict(headers)
        hops = 0
        pending: tuple[NormalizedUrl, Mapping[str, str]] | None = None
        last_fetched_url = url

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return http_408_deadline(hop.url, last_fetched_url, hop.started)

            admitted_headers = self._admit_hop(current_headers)
            await _flush_deferred_hop_cookies(pending, self._store_cookies)
            pending = None
            request = self._build_request(current_url, admitted_headers, remaining)

            try:
                result = await self._transport.fetch(request, max_bytes=max_bytes)
            except BodyTooLarge:
                return http_413(hop.url, current_url, hop.started)
            last_fetched_url = current_url

            next_hop, cookie_headers = await self._handle_hop_result(
                result, current_url, current_headers, hops
            )
            if next_hop is None:
                await self._store_cookies(current_url, cookie_headers)
                return result

            pending = (current_url, cookie_headers)
            current_url, current_headers = next_hop
            hops += 1

    @staticmethod
    def _admit_hop(headers: Mapping[str, str] | dict[str, str]) -> dict[str, str]:
        """Refuse injected header values before the prior hop's cookies are planted."""
        admitted = dict(headers)
        reject_injectable_header_values(admitted)
        return admitted

    @staticmethod
    def _build_request(url: NormalizedUrl, headers: dict[str, str], timeout_seconds: float) -> FetchRequest:
        """Build one logical hop; the proxy resolves and dials ``url``."""
        return FetchRequest(headers=headers, url=url, timeout_seconds=timeout_seconds)

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

    async def _advance(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        hops: int,
        result: HttpFetchResult,
    ) -> tuple[NormalizedUrl, dict[str, str]] | None:
        """The single redirect decision: ``redirect_target`` applied to hop state.

        Returns the next hop's ``(url, headers)``, or None when terminal; a second
        walk implementation used to decide this -- deleted, one chain one verdict.
        """
        return await redirect_target(url, headers, result.status_code, result.headers.get("location"), hops)


async def _flush_deferred_hop_cookies(
    deferred: tuple[NormalizedUrl, Mapping[str, str]] | None,
    store_cookies: StoreHopCookies,
) -> None:
    """Store the previous hop's Set-Cookie exactly once: reaching a fresh loop
    iteration means this hop's headers were admitted and the walk continues."""
    if deferred is not None:
        await store_cookies(*deferred)
