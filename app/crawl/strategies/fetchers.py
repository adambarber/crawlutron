"""Owns the direct httpx and curl_cffi fetch implementations.

This module owns bounded transport reads, decompression, redirects, and cookie persistence at
 the transport boundary. It must not interpret page content or construct crawl results.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from app.crawl.decompression import DecompressionError, DecompressionTooLargeError
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.strategies import http_client_pool, pinned_transport
from app.crawl.strategies.request_profile import _store_set_cookies
from app.models import HttpFetchResult
from app.timeouts import Timeouts
from app.validation import HostPolicy


async def _default_http_fetcher(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    *,
    policy: HostPolicy,
) -> HttpFetchResult:
    """Async HTTP/2 fetch using the shared pooled ``httpx.AsyncClient`` with
    granular timeouts (#4), SSRF protection (#7), content-length limits (#5) and
    SSRF-safe manual redirect handling (F1).

    Redirects are followed by hand so every hop is re-validated against the SSRF
    blocklist and re-pinned to its resolved IP. Automatic httpx following is off
    because it would connect to a redirect target without re-checking it.
    Sensitive headers (Cookie, Authorization) are stripped when crossing host
    boundaries to prevent credential leakage (Fix 3).
    """
    await http_client_pool._drain_pending_client_closes()
    client = http_client_pool.get_async_http_client()
    started = time.monotonic()
    timeout = Timeouts.for_request(timeout_seconds).as_httpx()
    current_url = url
    hops = 0
    # Work with a copy of headers so we can mutate per-hop
    request_headers = dict(headers)
    while True:
        pinned_url, hop_headers = await pinned_transport._build_pinned_request_async(
            current_url,
            request_headers,
            policy=policy,
        )
        # follow_redirects=False: we follow manually so each hop is SSRF-checked.
        async with client.stream(
            "GET", pinned_url, headers=hop_headers, timeout=timeout, follow_redirects=False
        ) as response:
            content_length = response.headers.get("content-length")
            if pinned_transport._declared_length_exceeds(content_length, max_content_length):
                return pinned_transport._http_413(url, current_url, started)
            status_code = response.status_code
            location = response.headers.get("location")
            # SSRF-safe redirect: only follow within the hop budget. The next
            # loop iteration re-runs _build_pinned_request, which re-validates
            # (and re-pins) the redirect target against the SSRF blocklist.
            redirect = pinned_transport._redirect_target(
                current_url,
                request_headers,
                status_code,
                location,
                hops,
                policy=policy,
            )
            if redirect is not None:
                current_url, request_headers = redirect
                hops += 1
                continue
            # Terminal response: stream RAW bytes, capping total read (#5).
            # bytearray.extend is O(chunk); ``raw += chunk`` on immutable
            # bytes re-allocates and copies the whole buffer per chunk.
            # A non-2xx streams through this same capped loop, so an error
            # body is bounded by max_content_length exactly like a success
            # body -- there is no separate status-error branch, and none
            # could fire: raise_for_status is never called, so httpx cannot
            # raise HTTPStatusError out of stream().
            raw = bytearray()
            async for chunk in response.aiter_raw():
                raw.extend(chunk)
                if len(raw) > max_content_length:
                    return pinned_transport._http_413(url, current_url, started)
            encoding = response.headers.get("content-encoding", "")
            try:
                body = _decompress_body(raw, encoding, max_output_bytes=max_content_length)
            except DecompressionTooLargeError:
                return pinned_transport._http_413(url, current_url, started)
            resp_headers = {key.lower(): value for key, value in response.headers.items()}
            # Already decompressed above; drop header to avoid a second pass.
            resp_headers.pop("content-encoding", None)
            final_url = current_url
        elapsed_ms = int((time.monotonic() - started) * 1000)
        _store_set_cookies(final_url or url, resp_headers)
        return HttpFetchResult(
            url=url,
            final_url=final_url,
            status_code=status_code,
            headers=resp_headers,
            body=body,
            elapsed_ms=elapsed_ms,
        )


async def _fallback_to_httpx(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int,
    *,
    policy: HostPolicy,
) -> HttpFetchResult:
    return await _default_http_fetcher(
        url, headers, timeout_seconds, max_content_length, policy=policy
    )


def _cffi_too_large_result(url: str, current_url: str, response: Any, elapsed_ms: int) -> HttpFetchResult:
    return HttpFetchResult(
        url=url,
        final_url=current_url,
        status_code=413,
        headers={key.lower(): value for key, value in response.headers.items()},
        body=b"",
        elapsed_ms=elapsed_ms,
    )


def _release_stream_noblock(response: Any) -> None:
    """Schedule a best-effort stream close without awaiting it (N-5).

    Callers are abandoning a live or wedged producer; awaiting
    ``response.aclose()`` would join that task and can hang (H1). The done
    callback consumes the result so a failed close never surfaces.
    """
    try:
        aclose = getattr(response, "aclose", None)
        if aclose is None:  # queue-contract fakes carry no close handle
            return
        task = asyncio.ensure_future(aclose())
    except RuntimeError:  # no running loop (tests driving the helper raw)
        return
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)


async def _default_cffi_fetcher(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    *,
    policy: HostPolicy,
) -> HttpFetchResult:
    """TLS-fingerprint-impersonated async HTTP fetcher using curl_cffi.

    Mirrors _default_http_fetcher, including SSRF-safe manual redirect handling
    (F1) and sensitive-header stripping on cross-domain redirects (Fix 3).
    Falls back to plain httpx on any import/fetch failure.
    """
    # Same release point as the http fetcher: any pooled client superseded by a
    # loop change (either kind) is closed here, on this live loop.
    await http_client_pool._drain_pending_client_closes()
    try:
        from curl_cffi.requests.exceptions import RequestException
        from curl_cffi.requests.models import STREAM_END
    except ImportError:
        return await _fallback_to_httpx(
            url, headers, timeout_seconds, max_content_length, policy=policy
        )
    # Shared session (perf throughput #4): built once per event loop instead of
    # per request. Unavailable (ImportError at build) -> plain httpx fallback.
    # NOTE: the client class is AsyncSession (curl_cffi>=0.7 API).
    # AsyncClient does not exist; referencing it raises AttributeError
    # and would silently degrade every impersonated fetch to plain httpx.
    client = http_client_pool.get_cffi_session()
    if client is None:
        return await _fallback_to_httpx(
            url, headers, timeout_seconds, max_content_length, policy=policy
        )
    try:
        current_url = url
        hops = 0
        started = time.monotonic()
        # Work with a copy of headers so we can mutate per-hop
        request_headers = dict(headers)
        while True:
            pinned_url, hop_headers = await pinned_transport._build_pinned_request_async(
            current_url,
            request_headers,
            policy=policy,
        )
            try:
                response = await client.get(
                    pinned_url,
                    headers=hop_headers,
                    allow_redirects=False,
                    stream=True,
                    timeout=float(timeout_seconds),
                )
            except Exception:
                # Fall back to plain httpx for this request on transport errors.
                return await _fallback_to_httpx(
                    url, headers, timeout_seconds, max_content_length, policy=policy
                )
            # Content-length check (#5)
            content_length = response.headers.get("content-length")
            if pinned_transport._declared_length_exceeds(content_length, max_content_length):
                await response.aclose()  # N8: release the curl handle
                return _cffi_too_large_result(
                    url, current_url, response, int((time.monotonic() - started) * 1000)
                )
            status_code = response.status_code
            location = response.headers.get("location")
            # SSRF-safe redirect: only follow within the hop budget; the next
            # iteration re-validates + re-pins the target via _build_pinned_request.
            redirect = pinned_transport._redirect_target(
                current_url,
                request_headers,
                status_code,
                location,
                hops,
                policy=policy,
            )
            if redirect is not None:
                await response.aclose()  # N8: release the curl handle
                current_url, request_headers = redirect
                hops += 1
                continue
            # N7: do not trust response.elapsed across curl_cffi versions
            # (timedelta in a requests-compatible build would TypeError
            # here and silently downgrade every fetch to httpx).
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Stream RAW bytes with a running cap (H1): a hostile origin
            # can omit content-length and stream gigabytes (chunked).
            # Abort with a 413 as soon as the cap is exceeded instead of
            # buffering the whole body into memory first.
            encoding = response.headers.get("content-encoding", "")
            raw = bytearray()
            # Consume the curl queue directly rather than
            # response.aiter_content(): on curl_cffi 0.16 the wrapper's
            # exception path awaits aclose(), which blocks forever on the
            # very stalled-stream error it is trying to re-raise (the
            # LOW_SPEED abort lands as a queue item but the wrapper wedges
            # before delivering it). Reading the queue keeps the abort
            # prompt and the failure reraise unblocked (H1).
            #
            # Every exit out of this loop except STREAM_END is an abandon, and
            # an abandoned handle is never reclaimed on its own now that the
            # session is shared per loop: the error re-raise below and a
            # cancellation of the queue wait (the engine's per-URL budget
            # cancels in-flight fetches routinely) both used to leave it busy.
            # ``stream_completed`` therefore gates a release-on-any-exit in the
            # finally, using the same non-blocking path the stall and cap
            # branches already used -- awaiting aclose here would join the
            # hostile producer being escaped (H1).
            stream_completed = False
            try:
                while True:
                    # Bound the wait PER CHUNK, not per response (N-5): this
                    # mirrors httpx's read-timeout semantics -- an inter-chunk
                    # gap limit. A slow-drip origin (one byte per interval)
                    # can keep the stream alive up to max_content_length; the
                    # per-URL budget above bounds the total. Do not "fix"
                    # this into a total deadline without also bounding drips.
                    # queue.get() is pure asyncio, so wait_for cancellation is
                    # safe here (unlike cancelling curl's internal machinery).
                    try:
                        chunk = await asyncio.wait_for(
                            response.queue.get(), timeout=max(timeout_seconds + 5.0, 10.0)
                        )
                    except TimeoutError as error:
                        # Stall: release the handle WITHOUT awaiting. aclose()
                        # joins the producer task, which is exactly the wedged
                        # task being escaped; awaiting it hangs like the old
                        # aiter_content() wrapper did (H1). A scheduled close
                        # reclaims the handle; the session client bounds any leak.
                        raise TimeoutError("cffi stream queue stalled") from error
                    if isinstance(chunk, RequestException):
                        raise chunk
                    if chunk is STREAM_END:
                        stream_completed = True
                        break
                    raw.extend(chunk)
                    if len(raw) > max_content_length:
                        # Mid-stream abandon: a 413 out of the loop is still an
                        # abandon, and the producer may be a hostile drip --
                        # never wait on it. The finally releases the handle.
                        return _cffi_too_large_result(url, current_url, response, elapsed_ms)
            finally:
                if not stream_completed:
                    _release_stream_noblock(response)
            # STREAM_END reached: the producer is done, so awaiting the close
            # is safe (unlike the stall/abandon paths above) and required now
            # that the session is shared -- the per-request `async with` used
            # to release the handle implicitly. Without this the next streamed
            # request on the shared session stalls on the busy handle (#4).
            await response.aclose()
            raw_bytes = bytes(raw)
            # Decompress with a size cap (decompression-bomb guard).
            try:
                body = _decompress_body(raw_bytes, encoding, max_output_bytes=max_content_length)
            except DecompressionTooLargeError:
                return _cffi_too_large_result(url, current_url, response, elapsed_ms)
            resp_headers = {key.lower(): value for key, value in response.headers.items()}
            resp_headers.pop("content-encoding", None)
            _store_set_cookies(current_url, response.headers)
            return HttpFetchResult(
                url=url,
                final_url=current_url,
                status_code=status_code,
                headers=resp_headers,
                body=body,
                elapsed_ms=elapsed_ms,
            )
    except DecompressionError:
        raise
    except Exception:  # any cffi failure falls back to plain httpx
        return await _fallback_to_httpx(
            url, headers, timeout_seconds, max_content_length, policy=policy
        )
