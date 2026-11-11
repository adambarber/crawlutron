"""curl_cffi transport: one pinned hop per ``fetch()`` call.

Mirrors the httpx transport's SSRF-safe redirect and header handling; raises on
transport failure so the caller can fall back to plain httpx.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Protocol, cast

from curl_cffi.requests.exceptions import RequestException, SSLError
from curl_cffi.requests.models import STREAM_END

from crawlutron.runtime.resource_lifecycle import record_declined_close
from crawlutron.safety.bounded_io import BodyTooLarge, read_capped_async
from crawlutron.transport import http_client_pool, pinned_transport, pooled_clients
from crawlutron.transport.timeouts import Timeouts
from crawlutron.transport.transport import (
    Closeable,
    FetchRequest,
    FetchResult,
    http_413,
    redirect_from_headers,
    terminal_from_response,
)


class _CffiResponse(Protocol):
    """curl_cffi response shape used by this transport: headers, status, close."""

    headers: Mapping
    queue: asyncio.Queue[object]
    status_code: int

    async def aclose(self) -> None: ...


async def _cffi_chunks(response: _CffiResponse, timeout_seconds: float) -> AsyncIterator[bytes]:
    """Yield curl_cffi queue chunks as bytes, ending at STREAM_END.

    A queued RequestException is raised; a stall raises TimeoutError so the
    caller's finally releases the handle without awaiting the producer.
    """
    while True:
        chunk = await asyncio.wait_for(
            response.queue.get(),
            timeout=max(Timeouts.for_request(timeout_seconds).read + 5.0, 10.0),
        )
        if isinstance(chunk, RequestException):
            raise chunk
        if chunk is STREAM_END:
            return
        yield cast(bytes, chunk)


def _stream_close_failure_recorder(response: Closeable) -> Callable[[asyncio.Task[None]], None]:
    def record(task: asyncio.Task[None]) -> None:
        if not task.cancelled() and task.exception() is not None:
            record_declined_close("stream_close_failed", type(response).__name__)

    return record


def release_stream_noblock(response: Closeable) -> None:
    """Schedule a best-effort stream close without awaiting it -- the caller is
    abandoning a live or wedged producer, and awaiting ``aclose`` would join that
    task and hang. The done callback swallows a failed close."""
    try:
        aclose = getattr(response, "aclose", None)
        if aclose is None:  # queue-contract fakes carry no close handle
            return
        task = asyncio.ensure_future(aclose())
    except RuntimeError:  # no running loop (tests driving the helper raw)
        return
    task.add_done_callback(_stream_close_failure_recorder(response))


class CffiTransport:
    """curl_cffi backend: one SSRF-pinned hop per ``fetch()`` call.

    Raises on transport failure so the caller can fall back to plain httpx;
    TLS-handshake failures always propagate (impersonation never completed).
    """

    def __init__(self, settings=None, release_stream_noblock=None) -> None:
        self._settings = settings
        self._release_stream_noblock = release_stream_noblock

    async def fetch(self, request: FetchRequest, *, max_bytes: int) -> FetchResult:
        """Send one pinned curl_cffi hop and return its result.

        Raises ``BodyTooLarge`` (the walker maps it to http_413); on transport
        failure raises so the caller can fall back to httpx.
        """
        started = time.monotonic()
        response, release = await self._dial(request)
        return await self._handle_response(response, request, started, max_bytes, release)

    async def _dial(
        self, request: FetchRequest
    ) -> tuple[_CffiResponse, Callable[[_CffiResponse], Awaitable[None]]]:
        """Dial one pinned hop; return the response and its release callable.
        Dial-phase failures close the one-shot session once before propagating.
        """
        await pooled_clients.drain_pending_client_closes()
        client = http_client_pool.get_cffi_session()

        one_shot = None
        dial_client = client
        wire_url = request.connect_url

        if request.resolve_entry is not None:
            dial_client = one_shot = http_client_pool.build_pinned_cffi_session(request.resolve_entry)
            # The wire URL stays the logical hostname: SNI and certificate
            # verification attest to it while curl connects to the pinned IP.
            wire_url = request.url.raw

        async def close_one_shot() -> None:
            """Close the one-shot pinned session exactly once, idempotently."""
            nonlocal one_shot
            if one_shot is not None:
                session = one_shot
                one_shot = None
                await session.close()

        try:
            response = await dial_client.get(
                wire_url,
                headers=request.headers,
                allow_redirects=False,
                stream=True,
                timeout=Timeouts.for_request(request.timeout_seconds).read,
            )
        except SSLError:
            # The handshake never completed, so there is no in-flight stream to
            # protect: release the one-shot session here and let SSLError propagate.
            await close_one_shot()
            raise
        except Exception:
            # Transport failure: the caller falls back to httpx. No in-flight
            # stream survived the failed dial, so release the one-shot session.
            await close_one_shot()
            raise
        except asyncio.CancelledError:
            # Dial-phase cancel: nothing drained the stream yet, so close the
            # one-shot session once and let the cancel propagate.
            await close_one_shot()
            raise

        async def release(response: _CffiResponse) -> None:
            """Release this hop: close the one-shot pinned session exactly once.

            The session stays alive until the terminal body is fully consumed, so
            this runs only after the drain (or abandon) is complete.
            """
            await close_one_shot()

        return response, release

    async def _handle_response(
        self,
        response: _CffiResponse,
        request: FetchRequest,
        started: float,
        max_bytes: int,
        release: Callable[[_CffiResponse], Awaitable[None]],
    ) -> FetchResult:
        """Content-length gate, redirect, or terminal body read for one cffi hop.

        The one-shot pinned session stays alive until the terminal body is fully
        consumed; ``release`` closes it exactly once on every exit path.
        """
        stream_release = self._release_stream_noblock or release_stream_noblock
        try:
            content_length = response.headers.get("content-length")
            if pinned_transport.declared_length_exceeds(content_length, max_bytes):
                await response.aclose()  # release the curl handle
                await release(response)  # close the one-shot session exactly once
                return http_413(request.url, request.url, started)

            status_code, location = response.status_code, response.headers.get("location")
            if 300 <= status_code < 400 and location is not None:
                # Redirect: do not read the body; the walker advances the hop.
                await response.aclose()  # release the curl handle
                await release(response)  # close the one-shot session exactly once
                return redirect_from_headers(request, status_code, started, response.headers)

            # Terminal: cap the body via read_capped_async (one cap, both transports).
            try:
                raw = await read_capped_async(_cffi_chunks(response, request.timeout_seconds), max_bytes)
            except BodyTooLarge:
                stream_release(response)
                await release(response)
                raise
            except (RequestException, asyncio.CancelledError):
                stream_release(response)
                await release(response)
                raise

            result = await terminal_from_response(
                response, request, started, max_bytes, response.headers, raw, status_code
            )
            await release(response)  # close the one-shot session exactly once
            return result
        except (BodyTooLarge, RequestException, asyncio.CancelledError):
            # Already released by the inner handler; just propagate.
            raise
        except Exception:
            # Best-effort release on any unexpected error.
            stream_release(response)
            await release(response)
            with contextlib.suppress(Exception):
                await response.aclose()
            raise
