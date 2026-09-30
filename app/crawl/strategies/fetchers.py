"""Owns the direct httpx and curl_cffi fetch implementations.

This module owns bounded transport reads, decompression, redirects, and cookie persistence at
 the transport boundary. It must not interpret page content or construct crawl results.
"""

from __future__ import annotations

import asyncio
import functools
import time
from collections.abc import Callable
from typing import Any

from curl_cffi.requests.exceptions import RequestException
from curl_cffi.requests.models import STREAM_END

from app.constants import DEFAULT_MAX_CONTENT_LENGTH_BYTES
from app.crawl.decompression import DecompressionError, DecompressionTooLargeError
from app.crawl.decompression import decompress_body as _decompress_body
from app.crawl.strategies import http_client_pool, pinned_transport
from app.crawl.strategies.request_profile import _store_set_cookies, _TransportHeaders
from app.models import HttpFetcher, HttpFetchResult
from app.timeouts import Timeouts
from app.validation import HostPolicy


async def _default_http_fetcher(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    *,
    policy: HostPolicy,
    impersonate: bool = False,
) -> HttpFetchResult:
    """The one fetcher: TLS impersonation first when asked, the pooled httpx client otherwise.

    ``impersonate`` is the whole transport preference, as one keyword on the one
    function that carries the contract. It used to be a second module-level
    entry point -- ``_default_cffi_fetcher`` -- which meant the contract's five
    parameters had to be spelled twice, and the twin could only be kept honest
    by a guard that walked the tree. The engine now binds the preference instead
    of the signature: ``partial(_default_http_fetcher, policy=..., impersonate=True)``
    is a different *binding* of the same callable, so a parameter can no longer
    be added to one role and missed on the other, because there is only one role
    and one spelling left.

    The impersonated attempt falls back to plain httpx, not to an error: the same
    page fetched differently is not a different outcome, and a 200 from the
    fallback is not a failure of the attempt that could not be made.

    Async HTTP/2 fetch over the shared pooled ``httpx.AsyncClient`` with
    granular timeouts, SSRF protection, content-length limits and
    SSRF-safe manual redirect handling.

    Redirects are followed by hand so every hop is re-validated against the SSRF
    blocklist and re-pinned to its resolved IP. Automatic httpx following is off
    because it would connect to a redirect target without re-checking it.
    Sensitive headers (Cookie, Authorization) are stripped when crossing host
    boundaries to prevent credential leakage.

    The signature is :data:`app.models.HttpFetcher` plus the two bound keywords,
    checked tree-wide by ``test_http_fetcher_contract_holds_tree_wide``.
    """
    if impersonate:
        attempted = await _cffi_fetch(url, headers, timeout_seconds, max_content_length, policy=policy)
        if attempted is not None:
            return attempted
    await http_client_pool._drain_pending_client_closes()
    client = http_client_pool.get_async_http_client()
    started = time.monotonic()
    timeout = Timeouts.for_request(timeout_seconds).as_httpx()
    async for chain, pinned_url, hop_headers in pinned_transport.apinned_hops(url, headers, policy=policy):
        # Follow_redirects=False: we follow manually so each hop is SSRF-checked.
        async with client.stream(
            "GET", pinned_url, headers=hop_headers, timeout=timeout, follow_redirects=False
        ) as response:
            content_length = response.headers.get("content-length")
            if pinned_transport._declared_length_exceeds(content_length, max_content_length):
                return pinned_transport._http_413(url, chain.url, started)
            status_code = response.status_code
            if chain.advance(response):
                continue
            # Terminal response: stream RAW bytes, capping total read.
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
                    return pinned_transport._http_413(url, chain.url, started)
            final_url = chain.url
            response_headers = response.headers
        elapsed_ms = int((time.monotonic() - started) * 1000)
        return _finish_fetch(
            url=url,
            final_url=final_url,
            status_code=status_code,
            raw=raw,
            response_headers=response_headers,
            max_content_length=max_content_length,
            elapsed_ms=elapsed_ms,
            too_large=functools.partial(pinned_transport._http_413, url, chain.url, started),
        )
    raise AssertionError("apinned_hops raises on a body that neither returned nor advanced")


def _finish_fetch(
    *,
    url: str,
    final_url: str,
    status_code: int,
    raw: bytes | bytearray,
    response_headers: _TransportHeaders,
    max_content_length: int,
    elapsed_ms: int,
    too_large: Callable[[], HttpFetchResult],
) -> HttpFetchResult:
    """Decompress, persist cookies and build the terminal result of a fetch.

    Both transports share this tail. The three things in it must not diverge:
    the decompression bomb cap, which is a safety bound, cookie persistence,
    and the header normalisation the shaping layer reads. The parts that
    genuinely differ per transport stay at the call site and arrive as the
    ``too_large`` *factory* -- httpx returns from inside its pooled client's
    ``async with``, while curl_cffi has to name a busy handle it already
    released. A factory rather than a value so the 413 is only built when a
    decompression bomb actually trips, not on every successful fetch.

    Cookies are stored from ``response_headers`` -- the transport's own
    multidict -- rather than from the normalised dict below. httpx's
    ``Headers.items()`` comma-joins duplicate keys, so normalising first
    collapses a multi-cookie response into one ``"a=1; Path=/, b=2; Path=/"``
    entry and forces ``_store_set_cookies`` off its documented ``get_list``
    path. The jar re-splits such a value today, but only when the first cookie
    carries its own attributes, and a comma-joined cookie value is a
    documented corruption risk in its own right.

    The content encoding is read here rather than passed in: both transports
    took it from the same header with the same expression, so a third reading
    of it was pure opportunity to disagree.
    """
    try:
        body = _decompress_body(
            raw,
            response_headers.get("content-encoding", ""),
            max_output_bytes=max_content_length,
        )
    except DecompressionTooLargeError:
        return too_large()
    _store_set_cookies(final_url or url, response_headers)
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


async def _cffi_fetch(
    url: str,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int,
    *,
    policy: HostPolicy,
) -> HttpFetchResult | None:
    """The curl_cffi transport behind ``_default_http_fetcher(impersonate=True)``.

    TLS-fingerprint-impersonated, mirroring the httpx transport including its
    SSRF-safe manual redirect handling and sensitive-header stripping on
    cross-domain redirects.

    Returns ``None`` -- not a result -- when the attempt failed for any
    transport reason, so the caller decides the fallback. A transport that
    cannot answer must not answer with an error result: the httpx path is the
    same page fetched differently, not a different outcome, and a 200 from the
    fallback is not a failure of the impersonated attempt.
    """
    # Same release point as the http fetcher: any pooled client superseded by a
    # loop change (either kind) is closed here, on this live loop.
    await http_client_pool._drain_pending_client_closes()
    # Shared session: built once per event loop instead of
    # per request. NOTE: the client class is AsyncSession (curl_cffi>=0.7 API).
    # AsyncClient does not exist; referencing it raises AttributeError
    # and would silently degrade every impersonated fetch to plain httpx.
    client = http_client_pool.get_cffi_session()
    try:
        started = time.monotonic()
        async for chain, pinned_url, hop_headers in pinned_transport.apinned_hops(
            url, headers, policy=policy
        ):
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
                return None
            # Content-length check
            content_length = response.headers.get("content-length")
            if pinned_transport._declared_length_exceeds(content_length, max_content_length):
                await response.aclose()  # release the curl handle
                return _cffi_too_large_result(
                    url, chain.url, response, int((time.monotonic() - started) * 1000)
                )
            status_code = response.status_code
            if chain.advance(response):
                await response.aclose()  # release the curl handle
                continue
            # Do not trust response.elapsed across curl_cffi versions
            # (timedelta in a requests-compatible build would TypeError
            # here and silently downgrade every fetch to httpx).
            elapsed_ms = int((time.monotonic() - started) * 1000)
            # Stream RAW bytes with a running cap: a hostile origin
            # can omit content-length and stream gigabytes (chunked).
            # Abort with a 413 as soon as the cap is exceeded instead of
            # buffering the whole body into memory first.
            raw = bytearray()
            # Consume the curl queue directly rather than
            # response.aiter_content(): on curl_cffi 0.16 the wrapper's
            # exception path awaits aclose(), which blocks forever on the
            # very stalled-stream error it is trying to re-raise (the
            # LOW_SPEED abort lands as a queue item but the wrapper wedges
            # before delivering it). Reading the queue keeps the abort
            # prompt and the failure reraise unblocked.
            #
            # Every exit out of this loop except STREAM_END is an abandon, and
            # an abandoned handle is never reclaimed on its own now that the
            # session is shared per loop: the error re-raise below and a
            # cancellation of the queue wait (the engine's per-URL budget
            # cancels in-flight fetches routinely) both used to leave it busy.
            # ``stream_completed`` therefore gates a release-on-any-exit in the
            # finally, using the same non-blocking path the stall and cap
            # branches already used -- awaiting aclose here would join the
            # hostile producer being escaped.
            stream_completed = False
            try:
                while True:
                    # Bound the wait PER CHUNK, not per response: this
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
                        # aiter_content() wrapper did. A scheduled close
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
                        return _cffi_too_large_result(url, chain.url, response, elapsed_ms)
            finally:
                if not stream_completed:
                    _release_stream_noblock(response)
            # STREAM_END reached: the producer is done, so awaiting the close
            # is safe (unlike the stall/abandon paths above) and required now
            # that the session is shared -- the per-request `async with` used
            # to release the handle implicitly. Without this the next streamed
            # request on the shared session stalls on the busy handle.
            await response.aclose()
            # Decompress with a size cap (decompression-bomb guard), normalise
            # headers and persist cookies via the shared tail.
            return _finish_fetch(
                url=url,
                final_url=chain.url,
                status_code=status_code,
                raw=bytes(raw),
                response_headers=response.headers,
                max_content_length=max_content_length,
                elapsed_ms=elapsed_ms,
                too_large=functools.partial(_cffi_too_large_result, url, chain.url, response, elapsed_ms),
            )
        raise AssertionError("apinned_hops raises on a body that neither returned nor advanced")
    except DecompressionError:
        raise
    except Exception:  # any cffi failure falls back to plain httpx
        return None


def impersonating_fetcher(policy: HostPolicy) -> HttpFetcher:
    """The one fetcher, bound to prefer TLS impersonation.

    This is the value the engine hands the chain for the impersonating strategy,
    and it is a *binding* rather than a second function so the contract's five
    parameters are spelled exactly once in the tree. It lives here, next to the
    function it binds, because it was previously written out three times: the
    engine reached for a second module-level entry point, the tests reached for
    that same entry point, and each of them re-decided independently which
    transport "the cffi fetcher" meant. One factory is the one place that
    decision is made.
    """
    return functools.partial(_default_http_fetcher, policy=policy, impersonate=True)


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
    """Schedule a best-effort stream close without awaiting it.

    Callers are abandoning a live or wedged producer; awaiting
    ``response.aclose()`` would join that task and can hang. The done
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
