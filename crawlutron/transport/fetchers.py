"""Owns the direct httpx and curl_cffi fetch implementations.

This module owns bounded transport reads, decompression, redirects, and cookie persistence at
 the transport boundary. It must not interpret page content or construct crawl results.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

import httpx
from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.models import STREAM_END

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.transport import http_client_pool, pinned_transport, pooled_clients
from crawlutron.transport.decompression import DecompressionError, DecompressionTooLargeError
from crawlutron.transport.decompression import decompress_body as _decompress_body
from crawlutron.transport.request_profile import TransportHeaders, store_set_cookies
from crawlutron.transport.timeouts import Timeouts
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

if TYPE_CHECKING:
    from curl_cffi.requests.models import Response as CffiResponse

logger = logging.getLogger(__name__)


async def default_http_fetcher(
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    *,
    impersonate: bool = False,
    pin: pinned_transport.PinRule | None = None,
    release_stream_noblock: Callable[[CffiResponse], None] | None = None,
) -> HttpFetchResult:
    """The one fetcher: TLS impersonation first when asked, the pooled httpx
    client otherwise; ``impersonate`` bound via ``partial`` so no twin can drift.
    """
    if impersonate:
        attempted = await cffi_fetch(
            url,
            headers,
            timeout_seconds,
            max_content_length,
            pin=pin,
            release_stream_noblock=release_stream_noblock,
        )
        if attempted is not None:
            return attempted
    await pooled_clients.drain_pending_client_closes()
    client = http_client_pool.get_async_http_client()
    started = time.monotonic()
    timeout = Timeouts.for_request(timeout_seconds).as_httpx()
    pending: tuple[NormalizedUrl, TransportHeaders] | None = None
    async for chain, pinned_url, hop_headers in pinned_transport.apinned_hops(
        url, headers, pin=pin, timeout_seconds=timeout_seconds
    ):
        # The previous hop's Set-Cookie flushes here: reaching this iteration
        # means THIS hop's pin admitted the walk.
        await _flush_deferred_hop_cookies(pending)
        pending = None
        terminal, pending = await _stream_terminal_hop(
            client,
            chain=chain,
            url=url,
            pinned_url=pinned_url,
            hop_headers=hop_headers,
            timeout=timeout,
            max_content_length=max_content_length,
            started=started,
        )
        if terminal is not None:
            return terminal
    raise AssertionError("apinned_hops raises on a body that neither returned nor advanced")


async def _stream_terminal_hop(
    client: httpx.AsyncClient,
    *,
    chain,
    url,
    pinned_url,
    hop_headers,
    timeout,
    max_content_length,
    started,
) -> tuple[HttpFetchResult | None, tuple[NormalizedUrl, TransportHeaders] | None]:
    """Send one SSRF-pinned hop; ``(None, pending_cookie_hop)`` when the hop is
    a redirect asking for another (re-pinned) request -- the pending tuple is
    stored only once the next hop's pin admits the walk.
    """
    async with pinned_transport.apinned_stream(client, chain, pinned_url, hop_headers, timeout) as response:
        content_length = response.headers.get("content-length")
        if pinned_transport.declared_length_exceeds(content_length, max_content_length):
            return http_413(url, chain.url, started), None
        status_code = response.status_code
        hop_url = chain.url
        if await chain.advance(response):
            # A real cookie (RFC 6265 5.3), but persistence is DEFERRED: stored
            # only after the next hop's pin admits the walk.
            return None, (hop_url, response.headers)
        raw = bytearray()
        async for chunk in response.aiter_raw():
            raw.extend(chunk)
            if len(raw) > max_content_length:
                return http_413(url, chain.url, started), None
        return (
            await _finish_fetch(
                url=url,
                final_url=chain.url,
                status_code=status_code,
                raw=raw,
                response_headers=response.headers,
                max_content_length=max_content_length,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                too_large=functools.partial(http_413, url, chain.url, started),
            ),
            None,
        )


async def _store_hop_cookies(hop_url: NormalizedUrl, response_headers: TransportHeaders) -> None:
    """The redirect-hop cookie door: same tail, one hop later."""
    await store_set_cookies(hop_url, response_headers)


async def _flush_deferred_hop_cookies(
    deferred: tuple[NormalizedUrl, TransportHeaders] | None,
) -> None:
    """Store the previous hop's Set-Cookie exactly once: reaching a fresh loop
    iteration means this hop's pin admitted the walk.
    """
    if deferred is not None:
        await _store_hop_cookies(*deferred)


async def _finish_fetch(
    *,
    url: NormalizedUrl,
    final_url: NormalizedUrl,
    status_code: int,
    raw: bytes | bytearray,
    response_headers: TransportHeaders,
    max_content_length: int,
    elapsed_ms: int,
    too_large: Callable[[], HttpFetchResult],
) -> HttpFetchResult:
    """Decompress, persist cookies, and build the terminal result -- the shared
    tail both transports use, so the bomb cap, cookie persistence, and header
    casing cannot diverge; ``too_large`` is a factory that runs only on a bomb.
    """
    try:
        body = _decompress_body(
            raw,
            response_headers.get("content-encoding", ""),
            max_output_bytes=max_content_length,
        )
    except DecompressionTooLargeError:
        return too_large()
    await store_set_cookies(final_url, response_headers)
    headers = {key.lower(): value for key, value in response_headers.items()}
    # Already decompressed above; drop the header to avoid a second pass.
    headers.pop("content-encoding", None)
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=status_code,
        headers=headers,
        body=body,
        elapsed_ms=elapsed_ms,
    )


async def cffi_fetch(
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int,
    *,
    release_stream_noblock: Callable[[CffiResponse], None] | None = None,
    pin: pinned_transport.PinRule | None = None,
) -> HttpFetchResult | None:
    """The curl_cffi transport behind ``impersonate=True``: TLS-impersonated,
    mirroring httpx's SSRF-safe redirects and header stripping. Returns ``None``,
    not a result, on any transport failure -- the httpx fallback is that outcome.
    """
    # Same release point as the http fetcher: any pooled client superseded by a
    # loop change (either kind) is closed here, on this live loop.
    await pooled_clients.drain_pending_client_closes()
    # One shared session per event loop. The class is AsyncSession
    # (curl_cffi>=0.7): AsyncClient does not exist, and naming it breaks every fetch.
    client = http_client_pool.get_cffi_session()
    # Declared outside the walk so the twin loops share no longer a window.
    pending: tuple[NormalizedUrl, TransportHeaders] | None = None
    try:
        started = time.monotonic()
        async for chain, pinned_url, hop_headers in pinned_transport.apinned_hops(
            url, headers, pin=pin, timeout_seconds=timeout_seconds
        ):
            # Same door as the httpx twin: the previous hop's Set-Cookie
            # flushes only after THIS hop's pin admitted the walk.
            await _flush_deferred_hop_cookies(pending)
            pending = None
            handled, pending = await _handle_cffi_hop(
                url,
                chain,
                client,
                pinned_url,
                hop_headers,
                timeout_seconds=timeout_seconds,
                max_content_length=max_content_length,
                started=started,
                release_stream_noblock=release_stream_noblock,
            )
            if handled is not None:
                return handled
        raise AssertionError("apinned_hops raises on a body that neither returned nor advanced")
    except DecompressionError:
        raise
    except SSLError:
        # A TLS-handshake failure means impersonation never completed: serving
        # the page via plain httpx would be a silent capability lie.
        raise
    except Exception as error:  # other cffi failures fall back, but visibly
        logger.warning(
            "cffi_fetch_fallback",
            extra={"url": url.raw, "error_code": type(error).__name__},
        )
        return None


async def _handle_cffi_hop(
    url: NormalizedUrl,
    chain,
    client,
    pinned_url: str,
    hop_headers: dict[str, str],
    *,
    timeout_seconds: int,
    max_content_length: int,
    started: float,
    release_stream_noblock: Callable[[CffiResponse], None] | None,
) -> tuple[HttpFetchResult | None, tuple[NormalizedUrl, TransportHeaders] | None]:
    """One pinned hop: GET, too-large gate, redirect advance, or streamed body;
    the redirect arm returns its pending cookie hop instead of storing it.
    """
    try:
        response = await client.get(
            pinned_url,
            headers=hop_headers,
            allow_redirects=False,
            stream=True,
            timeout=Timeouts.for_request(timeout_seconds).read,
        )
    except SSLError:
        # A TLS-handshake failure means impersonation never completed: serving
        # the page via plain httpx would be a silent capability lie.
        raise
    except Exception:
        # Fall back to plain httpx for this request on transport errors.
        return None, None
    # Content-length check
    content_length = response.headers.get("content-length")
    if pinned_transport.declared_length_exceeds(content_length, max_content_length):
        await response.aclose()  # release the curl handle
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return _cffi_too_large_result(url, chain.url, response, elapsed_ms), None
    if await chain.advance(response):
        await response.aclose()  # release the curl handle
        # Deferred like the httpx twin: stored only when the next pin admits.
        return None, (chain.url, response.headers)
    # Never trust response.elapsed across curl_cffi versions: a timedelta
    # there TypeErrors and silently downgrades every fetch to plain httpx.
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return (
        await _read_capped_stream(
            url,
            chain,
            response,
            timeout_seconds=timeout_seconds,
            max_content_length=max_content_length,
            elapsed_ms=elapsed_ms,
            release_stream_noblock=release_stream_noblock,
        ),
        None,
    )


async def _read_capped_stream(
    url: NormalizedUrl,
    chain,
    response,
    *,
    timeout_seconds: int,
    max_content_length: int,
    elapsed_ms: int,
    release_stream_noblock: Callable[[CffiResponse], None] | None,
) -> HttpFetchResult | None:
    """Stream RAW bytes with a running cap and finish the fetch.

    A hostile origin can omit content-length and stream gigabytes; abort with
    a 413 past the cap. The curl queue is read directly; every exit releases it.
    """
    raw = bytearray()
    completed = False
    early_result: HttpFetchResult | None = None
    try:
        early_result = await _drain_capped_stream(
            response,
            raw,
            timeout_seconds=timeout_seconds,
            max_content_length=max_content_length,
            too_large=lambda: _cffi_too_large_result(url, chain.url, response, elapsed_ms),
        )
        completed = early_result is None
    finally:
        if not completed:
            if release_stream_noblock is not None:
                release_stream_noblock(response)
            else:
                _release_stream_noblock(response)
    if early_result is not None:
        return early_result
    # STREAM_END means the producer is done, so awaiting close is safe -- and
    # now required: an unreleased handle stalls the next streamed request.
    await response.aclose()
    # Decompress with a size cap (decompression-bomb guard), normalise
    # headers and persist cookies via the shared tail.
    return await _finish_fetch(
        url=url,
        final_url=chain.url,
        status_code=response.status_code,
        raw=bytes(raw),
        response_headers=response.headers,
        max_content_length=max_content_length,
        elapsed_ms=elapsed_ms,
        too_large=functools.partial(
            _cffi_too_large_result,
            url,
            chain.url,
            response,
            elapsed_ms,
        ),
    )


async def _drain_capped_stream(
    response,
    raw: bytearray,
    *,
    timeout_seconds: int,
    max_content_length: int,
    too_large: Callable[[], HttpFetchResult | None],
) -> HttpFetchResult | None:
    """Fill ``raw`` from the curl queue under the cap; None only on STREAM_END.

    Non-None is the early 413 result (the abandon path never awaits the
    producer). A stall raises so the caller's finally releases WITHOUT awaiting.
    """
    while True:
        try:
            chunk = await asyncio.wait_for(
                response.queue.get(),
                timeout=max(Timeouts.for_request(timeout_seconds).read + 5.0, 10.0),
            )
        except TimeoutError as error:
            raise TimeoutError("cffi stream queue stalled") from error
        if isinstance(chunk, RequestException):
            raise chunk
        if chunk is STREAM_END:
            return None
        raw.extend(chunk)
        if len(raw) > max_content_length:
            return too_large()


def impersonating_fetcher() -> HttpFetcher:
    """The one fetcher bound to prefer TLS impersonation -- a ``partial`` binding,
    not a second function, so the contract is spelled once in the tree. Lives
    here, next to the function it binds, as the one place that decision is made.
    """
    return functools.partial(default_http_fetcher, impersonate=True)


def http_413(url: NormalizedUrl, final_url: NormalizedUrl, started: float) -> HttpFetchResult:
    """Standard 413 (payload too large) result for the httpx size/cap violations.

    Takes the logical ``final_url``, never ``current_url``: a pinned plain-HTTP
    response url is the resolved-IP literal, which leaks topology and mis-keys cookies by IP.
    """
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=413,
        headers={"content-type": "application/octet-stream"},
        body=b"",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _cffi_too_large_result(
    url: NormalizedUrl, current_url: NormalizedUrl, response: CffiResponse, elapsed_ms: int
) -> HttpFetchResult:
    # Synthetic headers, never echoed: echoing content-encoding turned the
    # empty refusal body into a decompression fault instead of the size verdict.
    return HttpFetchResult(
        url=url,
        final_url=current_url,
        status_code=413,
        headers={"content-type": "application/octet-stream"},
        body=b"",
        elapsed_ms=elapsed_ms,
    )


def _release_stream_noblock(response: CffiResponse) -> None:
    """Schedule a best-effort stream close without awaiting it -- the caller is
    abandoning a live or wedged producer, and awaiting ``aclose`` would join that
    task and hang. The done callback swallows a failed close.
    """
    try:
        aclose = getattr(response, "aclose", None)
        if aclose is None:  # queue-contract fakes carry no close handle
            return
        task = asyncio.ensure_future(aclose())
    except RuntimeError:  # no running loop (tests driving the helper raw)
        return
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
