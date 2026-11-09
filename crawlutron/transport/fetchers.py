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
from dataclasses import dataclass
from typing import TYPE_CHECKING

import httpx
from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.models import STREAM_END

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.runtime.resource_lifecycle import record_declined_close
from crawlutron.safety.url_redaction import redact_url
from crawlutron.transport import http_client_pool, pinned_transport, pooled_clients
from crawlutron.transport.decompression import DecompressionError, DecompressionTooLargeError
from crawlutron.transport.decompression import decompress_body as _decompress_body
from crawlutron.transport.request_profile import TransportHeaders, store_set_cookies
from crawlutron.transport.timeouts import Timeouts
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

if TYPE_CHECKING:
    from curl_cffi.requests.models import Response as CffiResponse

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CffiHop:
    """One cffi walk's shared frame: the parameters every hop arm forwards.

    The walk's arms (GET, too-large gate, redirect advance, stream read) all
    speak the same request identity, so it travels as one value instead of a
    six-parameter echo between the hop handler and its arms.
    """

    url: NormalizedUrl
    timeout_seconds: int
    max_content_length: int
    started: float
    release_stream_noblock: Callable[[CffiResponse], None] | None

    async def stream_read(self, chain, response) -> HttpFetchResult:
        """Stream RAW curl bytes under the cap, then finish the fetch.

        A hostile origin can omit content-length and stream gigabytes, so the
        queue is read directly and every non-terminal exit releases the handle
        without awaiting it.
        """
        raw = bytearray()
        completed = False
        early_result: HttpFetchResult | None = None
        try:
            early_result = await _drain_capped_stream(
                response,
                raw,
                timeout_seconds=self.timeout_seconds,
                max_content_length=self.max_content_length,
                too_large=functools.partial(http_413, self.url, chain.url, self.started),
            )
            completed = early_result is None
        finally:
            if not completed:
                if self.release_stream_noblock is not None:
                    self.release_stream_noblock(response)
                else:
                    release_stream_noblock(response)
        if early_result is not None:
            return early_result
        # STREAM_END means the producer is done, so awaiting close is safe -- and
        # now required: an unreleased handle stalls the next streamed request.
        await response.aclose()
        return await _finish_fetch(
            url=self.url,
            chain=chain,
            response=response,
            raw=bytes(raw),
            status_code=response.status_code,
            started=self.started,
            max_content_length=self.max_content_length,
        )


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
    async for chain, pinned in pinned_transport.apinned_hops(
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
            pinned=pinned,
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
    pinned,
    timeout,
    max_content_length,
    started,
) -> tuple[HttpFetchResult | None, tuple[NormalizedUrl, TransportHeaders] | None]:
    """Send one SSRF-pinned hop; ``(None, pending_cookie_hop)`` when the hop is
    a redirect asking for another (re-pinned) request -- the pending tuple is
    stored only once the next hop's pin admits the walk.
    """
    async with pinned_transport.apinned_stream(client, chain, pinned, timeout) as response:
        content_length = response.headers.get("content-length")
        if pinned_transport.declared_length_exceeds(content_length, max_content_length):
            return http_413(url, chain.url, started), None
        status_code = response.status_code
        served_url = await chain.advance(response)
        if served_url is not None:
            # A real cookie (RFC 6265 5.3), but persistence is DEFERRED: stored
            # only after the next hop's pin admits the walk.
            return None, (served_url, response.headers)
        raw = bytearray()
        async for chunk in response.aiter_raw():
            raw.extend(chunk)
            if len(raw) > max_content_length:
                return http_413(url, chain.url, started), None
        return (
            await _finish_fetch(
                url=url,
                chain=chain,
                response=response,
                raw=raw,
                status_code=status_code,
                started=started,
                max_content_length=max_content_length,
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
    chain,
    response,
    raw: bytes | bytearray,
    status_code: int,
    started: float,
    max_content_length: int,
) -> HttpFetchResult:
    """Decompress, persist cookies, and build the terminal result -- the shared
    tail both transports use, so the bomb cap, cookie persistence, and header
    casing cannot diverge. ``chain``/``response`` arrive pre-advance: the final
    URL and headers are the terminal hop's own, and the 413 factory runs only
    on a bomb.
    """
    too_large = functools.partial(http_413, url, chain.url, started)
    try:
        body = _decompress_body(
            raw,
            response.headers.get("content-encoding", ""),
            max_output_bytes=max_content_length,
        )
    except DecompressionTooLargeError:
        return too_large()
    await store_set_cookies(chain.url, response.headers)
    headers = {key.lower(): value for key, value in response.headers.items()}
    # Already decompressed above; drop the header to avoid a second pass.
    headers.pop("content-encoding", None)
    return HttpFetchResult(
        url=url,
        final_url=chain.url,
        status_code=status_code,
        headers=headers,
        body=body,
        elapsed_ms=int((time.monotonic() - started) * 1000),
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
    hop = _CffiHop(
        url=url,
        timeout_seconds=timeout_seconds,
        max_content_length=max_content_length,
        started=time.monotonic(),
        release_stream_noblock=release_stream_noblock,
    )
    try:
        async for chain, pinned in pinned_transport.apinned_hops(
            url, headers, pin=pin, timeout_seconds=timeout_seconds
        ):
            # Same door as the httpx twin: the previous hop's Set-Cookie
            # flushes only after THIS hop's pin admitted the walk.
            await _flush_deferred_hop_cookies(pending)
            pending = None
            handled, pending = await _handle_cffi_hop(hop, chain, client, pinned)
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
            extra={"url": redact_url(url.raw), "error_code": type(error).__name__},
        )
        return None


async def _handle_cffi_hop(
    hop: _CffiHop,
    chain,
    client,
    pinned,
) -> tuple[HttpFetchResult | None, tuple[NormalizedUrl, TransportHeaders] | None]:
    """One pinned hop: GET, too-large gate, redirect advance, or streamed body;
    the redirect arm returns its pending cookie hop instead of storing it.
    """
    # A pinned HTTPS hop dials through a one-shot session carrying the resolve
    # table: the wire URL stays the logical hostname, so SNI and certificate
    # verification attest to it while curl connects to the pinned IP (#574).
    one_shot = None
    dial_client = client
    wire_url = pinned.connect_url
    resolve_entry = pinned_transport.cffi_resolve_entry(pinned)
    if resolve_entry is not None:
        dial_client = one_shot = http_client_pool.build_pinned_cffi_session(resolve_entry)
        # The wire URL stays the logical hostname: SNI and certificate
        # verification attest to it while curl connects to the pinned IP.
        wire_url = pinned.url.raw
    try:
        response = await dial_client.get(
            wire_url,
            headers=pinned.headers,
            allow_redirects=False,
            stream=True,
            timeout=Timeouts.for_request(hop.timeout_seconds).read,
        )
    except SSLError:
        # A TLS-handshake failure means impersonation never completed: serving
        # the page via plain httpx would be a silent capability lie.
        raise
    except Exception:
        # Fall back to plain httpx for this request on transport errors.
        return None, None
    finally:
        if one_shot is not None:
            await one_shot.close()
    # Content-length check
    content_length = response.headers.get("content-length")
    if pinned_transport.declared_length_exceeds(content_length, hop.max_content_length):
        await response.aclose()  # release the curl handle
        return http_413(hop.url, chain.url, hop.started), None
    served_url = await chain.advance(response)
    if served_url is not None:
        await response.aclose()  # release the curl handle
        # Deferred like the httpx twin: stored only when the next pin admits.
        return None, (served_url, response.headers)
    # Never trust response.elapsed across curl_cffi versions: a timedelta
    # there TypeErrors and silently downgrades every fetch to plain httpx.
    return await hop.stream_read(chain, response), None


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


def _stream_close_failure_recorder(response: CffiResponse) -> Callable[[asyncio.Task[None]], None]:
    """The done callback: retrieve (warning suppression stays load-bearing) and
    record a failed close in the #643 decline record; a cancelled close is a
    handled state, not evidence.
    """

    def record(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            record_declined_close("stream_close_failed", type(response).__name__)

    return record


def release_stream_noblock(response: CffiResponse) -> None:
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
    task.add_done_callback(_stream_close_failure_recorder(response))
