"""HTTP transport: one pinned hop over the pooled httpx client."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from crawlutron.safety.bounded_io import read_capped_async
from crawlutron.transport import http_client_pool, pinned_transport
from crawlutron.transport.timeouts import Timeouts
from crawlutron.transport.transport import (
    FetchRequest,
    FetchResult,
    http_413,
    redirect_from_headers,
    terminal_from_response,
)

if TYPE_CHECKING:
    from crawlutron.config.settings import CrawlerSettings


class HttpxTransport:
    """httpx backend: one SSRF-pinned hop per ``fetch()`` call.

    Redirects are not followed here -- the RedirectWalker owns the walk; this
    transport reads and caps the body, decompresses, and persists cookies.
    """

    def __init__(self, settings: CrawlerSettings | None = None) -> None:
        self._settings = settings

    async def fetch(self, request: FetchRequest, *, max_bytes: int) -> FetchResult:
        """Send one pinned httpx hop and return its result.

        Raises ``BodyTooLarge`` when the streamed body exceeds ``max_bytes``;
        the walker maps that to an http_413 ``FetchResult``.
        """
        client = http_client_pool.get_async_http_client()
        timeout = Timeouts.for_request(request.timeout_seconds).as_httpx()
        started = time.monotonic()

        extensions = None
        sni = request.url.tls_hostname
        if sni is not None:
            extensions = {"sni_hostname": sni}

        async with client.stream(
            "GET",
            request.connect_url,
            headers=request.headers,
            timeout=timeout,
            follow_redirects=False,
            extensions=extensions,
        ) as response:
            content_length = response.headers.get("content-length")
            if pinned_transport.declared_length_exceeds(content_length, max_bytes):
                return http_413(request.url, request.url, started)

            status_code = response.status_code
            location = response.headers.get("location")
            if 300 <= status_code < 400 and location is not None:
                # Redirect: do not read the body; the walker advances the hop.
                return redirect_from_headers(request, status_code, started, response.headers)

            raw = await read_capped_async(response.aiter_raw(), max_bytes)
            return await terminal_from_response(
                response, request, started, max_bytes, response.headers, raw, status_code
            )
