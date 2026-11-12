"""Transport protocol and result types for the unified fetch walk.

The :class:`Transport` protocol owns a single SSRF-pinned hop; the walker owns
the walk. Transports raise ``BodyTooLarge`` and the walker maps it to http_413."""

from __future__ import annotations

import functools
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.transport.decompression import (
    DecompressionTooLargeError,
)
from crawlutron.transport.decompression import (
    decompress_body as _decompress_body,
)

#: The result of one transport hop -- read by the walk layer as
#: url/final_url/status_code/headers/body/elapsed_ms.
FetchResult = HttpFetchResult


class Closeable(Protocol):
    """Minimal interface: anything with an async ``aclose()``. Both httpx and
    curl_cffi responses satisfy this, so transports can share ``terminal_fetch_result``
    without importing either client's concrete response type at runtime."""

    async def aclose(self) -> None: ...


@dataclass(frozen=True, slots=True)
class FetchRequest:
    """One hop's admission: the wire URL to dial, the headers to send, the
    logical URL (for results and cookies), the per-request timeout, and the
    optional curl_cffi resolve table for pinned HTTPS hops."""

    connect_url: str
    headers: dict[str, str]
    url: NormalizedUrl
    timeout_seconds: float
    resolve_entry: list[str] | None = None


class Transport(Protocol):
    """One pinned hop: send the request, cap the body, return a FetchResult.
    Implementations read the body with ``read_capped_async`` so capping lives in
    one place across both backends; on cap breach they raise ``BodyTooLarge``.
    """

    async def fetch(self, request: FetchRequest, *, max_bytes: int) -> FetchResult: ...


def http_413(url: NormalizedUrl, final_url: NormalizedUrl, started: float) -> HttpFetchResult:
    """Standard 413 (payload too large) result.

    Takes the logical ``final_url``, never ``current_url``: a pinned plain-HTTP
    response url is the resolved-IP literal, which leaks topology."""
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=413,
        headers={"content-type": "application/octet-stream"},
        body=b"",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def _lowercased(headers: Mapping[str, str]) -> dict[str, str]:
    """Lowercase every header key; values pass through unchanged."""
    return {key.lower(): value for key, value in headers.items()}


def redirect_fetch_result(
    request: FetchRequest,
    status_code: int,
    started: float,
    headers: dict[str, str],
    raw_headers: Mapping[str, str] | None,
) -> FetchResult:
    """FetchResult for a redirect response: no body, headers passed through."""
    return FetchResult(
        url=request.url,
        final_url=request.url,
        status_code=status_code,
        headers=headers,
        body=b"",
        elapsed_ms=int((time.monotonic() - started) * 1000),
        _raw_headers=raw_headers,
    )


def redirect_from_headers(
    request: FetchRequest,
    status_code: int,
    started: float,
    headers: Mapping[str, str],
) -> FetchResult:
    """Build a redirect ``FetchResult`` from a transport's raw headers multidict.

    Lowers the keys for the result dict while preserving the original multidict
    for cookie capture via ``get_list``.
    """
    return redirect_fetch_result(
        request,
        status_code,
        started,
        _lowercased(headers),
        headers,
    )


async def finish_fetch(
    *,
    url: NormalizedUrl,
    final_url: NormalizedUrl,
    headers: dict[str, str],
    started: float,
    raw_headers: Mapping[str, str] | None,
    raw: bytes | bytearray,
    status_code: int,
    max_bytes: int,
) -> HttpFetchResult:
    """Decompress and build the terminal result -- shared tail used by both
    transports so decompression and header casing cannot diverge. Cookie
    persistence is the walker's job (store_hop_cookies), not this tail's."""
    too_large = functools.partial(http_413, url, final_url, started)
    try:
        body = _decompress_body(raw, headers.get("content-encoding", ""), max_output_bytes=max_bytes)
    except DecompressionTooLargeError:
        return too_large()
    result_headers = {key: value for key, value in headers.items()}
    result_headers.pop("content-encoding", None)
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=status_code,
        headers=result_headers,
        body=body,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        _raw_headers=raw_headers,
    )


async def terminal_fetch_result(
    response: Closeable,
    request: FetchRequest,
    started: float,
    max_bytes: int,
    headers: dict[str, str],
    raw_headers: Mapping[str, str] | None,
    raw: bytes | bytearray,
    status_code: int,
) -> FetchResult:
    """Terminal hop result: release the response and run finish_fetch."""
    await response.aclose()
    return await finish_fetch(
        url=request.url,
        final_url=request.url,
        headers=headers,
        started=started,
        raw_headers=raw_headers,
        raw=raw,
        status_code=status_code,
        max_bytes=max_bytes,
    )


async def terminal_from_response(
    response: Closeable,
    request: FetchRequest,
    started: float,
    max_bytes: int,
    headers: Mapping[str, str],
    raw: bytes | bytearray,
    status_code: int,
) -> FetchResult:
    """Terminal hop result from a transport's raw headers multidict.

    Lowers the keys for the result dict while preserving the original multidict
    for cookie capture via ``get_list``.
    """
    return await terminal_fetch_result(
        response,
        request,
        started,
        max_bytes,
        _lowercased(headers),
        headers,
        raw,
        status_code,
    )
