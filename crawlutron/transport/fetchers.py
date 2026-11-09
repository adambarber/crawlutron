"""Owns the direct httpx and curl_cffi fetch implementations.

Delegates to :class:`~crawlutron.transport.redirect_walker.RedirectWalker`,
which owns the walk (redirects, cookie capture, pin validation, header checks)
calling ``transport.fetch()`` per hop. Capping is consolidated in
:func:`crawlutron.safety.bounded_io.read_capped_async` via the transports; cookie
capture lives in the walker only, which makes the C1 cookie-injection bug
structurally impossible. ~100 lines of parallel per-hop logic deleted from this
module.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Mapping

from curl_cffi.requests.exceptions import SSLError

from crawlutron.models.http_fetch_result import HttpFetchResult
from crawlutron.models.http_fetcher import HttpFetcher
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.url_redaction import redact_url
from crawlutron.transport import pinned_transport, pooled_clients
from crawlutron.transport.cffi_transport import CffiTransport
from crawlutron.transport.httpx_transport import HttpxTransport
from crawlutron.transport.redirect_walker import RedirectWalker
from crawlutron.transport.request_profile import store_set_cookies
from crawlutron.vocabulary.limits import DEFAULT_MAX_CONTENT_LENGTH_BYTES

logger = logging.getLogger(__name__)


async def default_http_fetcher(
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int = DEFAULT_MAX_CONTENT_LENGTH_BYTES,
    *,
    impersonate: bool = False,
    pin: pinned_transport.PinRule | None = None,
    release_stream_noblock: Callable[[object], None] | None = None,
) -> HttpFetchResult:
    """The one fetcher: TLS impersonation first when asked (falling back to httpx
    on transport failure), the pooled httpx client otherwise.

    ``release_stream_noblock`` is accepted for interface compatibility; stream
    release is handled inside the transport implementations now.
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
    result = await RedirectWalker(HttpxTransport(), store_cookies=store_hop_cookies).walk(
        url, headers, timeout_seconds, max_bytes=max_content_length, pin=pin
    )
    if result is None:
        # The httpx transport always returns a terminal result.
        raise AssertionError("httpx transport returned no terminal result")
    return result


async def cffi_fetch(
    url: NormalizedUrl,
    headers: dict[str, str],
    timeout_seconds: int,
    max_content_length: int,
    *,
    release_stream_noblock: Callable[[object], None] | None = None,
    pin: pinned_transport.PinRule | None = None,
) -> HttpFetchResult | None:
    """The curl_cffi transport behind impersonate=True. Returns None on
    transport failure so the httpx fallback can serve. TLS-handshake failures
    propagate -- impersonation never completed."""
    # Release superseded clients before any fetch work (pin/DNS included), so a
    # failed pin never leaves a displaced session ownerless -- the same ordering
    # the walk's httpx twin keeps in default_http_fetcher.
    await pooled_clients.drain_pending_client_closes()
    try:
        return await RedirectWalker(
            CffiTransport(release_stream_noblock=release_stream_noblock),
            store_cookies=store_hop_cookies,
        ).walk(url, headers, timeout_seconds, max_bytes=max_content_length, pin=pin)
    except SSLError:
        raise
    except Exception as error:  # other cffi failures fall back, but visibly
        logger.warning(
            "cffi_fetch_fallback",
            extra={"url": redact_url(url.raw), "error_code": type(error).__name__},
        )
        return None


def impersonating_fetcher() -> HttpFetcher:
    """The one fetcher bound to prefer TLS impersonation -- a ``partial`` binding,
    not a second function, so the contract is spelled once in the tree."""
    return functools.partial(default_http_fetcher, impersonate=True)


async def _store_hop_cookies(hop_url: NormalizedUrl, response_headers: Mapping[str, str]) -> None:
    """The redirect-hop cookie door: same tail, one hop later."""
    await store_set_cookies(hop_url, response_headers)


# Public alias so the walker can reach the sanctioned cookie tail without
# importing a private name across module boundaries.
store_hop_cookies = _store_hop_cookies
