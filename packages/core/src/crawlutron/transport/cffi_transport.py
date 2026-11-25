"""curl_cffi transport: one proxied hop per fetch() call.

reads time.monotonic() for fetch timing

wall-consumer
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Callable, Mapping
from typing import Protocol, cast

from curl_cffi.requests.exceptions import RequestException
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
    """curl_cffi backend: one proxied hop per ``fetch()`` call.

    Raises on transport failure so the caller can fall back to plain httpx;
    TLS-handshake failures always propagate (impersonation never completed).
    """

    def __init__(self, settings=None, release_stream_noblock=None) -> None:
        self._settings = settings
        self._release_stream_noblock = release_stream_noblock

    async def fetch(self, request: FetchRequest, *, max_bytes: int) -> FetchResult:
        """Send one proxied curl_cffi hop and return its result.

        Raises ``BodyTooLarge`` (the walker maps it to http_413); on transport
        failure raises so the caller can fall back to httpx.
        """
        started = time.monotonic()
        response = await self._dial(request)
        return await self._handle_response(response, request, started, max_bytes)

    async def _dial(self, request: FetchRequest) -> _CffiResponse:
        """Dial one hop through the shared proxied session; return the response."""
        await pooled_clients.drain_pending_client_closes()
        client = http_client_pool.get_cffi_session()
        # The proxy owns pinning and dialing; the wire keeps the logical URL so
        # SNI and certificate verification attest to the caller's hostname.
        response = await client.get(
            request.url.raw,
            headers=request.headers,
            allow_redirects=False,
            stream=True,
            timeout=Timeouts.for_request(request.timeout_seconds).read,
        )
        # Check for egress proxy dial-failed marker: the proxy refused to reach
        # the pinned target, so treat this as a transport failure for fallback.
        if response.headers.get("x-crawlutron-egress-refusal") == "dial-failed":
            await response.aclose()
            raise RequestException("egress proxy could not reach the pinned target")
        # curl_cffi 0.17.0 types ``queue`` as a sync queue.Queue (models.py:130) yet
        # awaits it on its async path (models.py:405); the runtime object satisfies this.
        return cast(_CffiResponse, response)

    async def _handle_response(
        self,
        response: _CffiResponse,
        request: FetchRequest,
        started: float,
        max_bytes: int,
    ) -> FetchResult:
        """Content-length gate, redirect, or terminal body read for one cffi hop."""
        stream_release = self._release_stream_noblock or release_stream_noblock
        try:
            content_length = response.headers.get("content-length")
            if pinned_transport.declared_length_exceeds(content_length, max_bytes):
                await response.aclose()
                return http_413(request.url, request.url, started)

            status_code, location = response.status_code, response.headers.get("location")
            if 300 <= status_code < 400 and location is not None:
                # Redirect: do not read the body; the walker advances the hop.
                await response.aclose()
                return redirect_from_headers(request, status_code, started, response.headers)

            # Terminal: cap the body via read_capped_async (one cap, both transports).
            try:
                raw = await read_capped_async(_cffi_chunks(response, request.timeout_seconds), max_bytes)
            except BodyTooLarge:
                stream_release(response)
                raise
            except (RequestException, asyncio.CancelledError):
                stream_release(response)
                raise

            return await terminal_from_response(
                response, request, started, max_bytes, response.headers, raw, status_code
            )
        except (BodyTooLarge, RequestException, asyncio.CancelledError):
            raise
        except Exception:
            # Best-effort release on any unexpected error.
            stream_release(response)
            with contextlib.suppress(Exception):
                await response.aclose()
            raise
