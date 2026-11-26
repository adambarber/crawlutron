"""Redirect targeting and request-header safety for the logical fetch walk.

The proxy owns target admission, fresh DNS and the only dial; nothing here
resolves a hostname or composes a connect URL.
"""

from __future__ import annotations

import httpx

from crawlutron.safety.header_authority import survives_cross_origin
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE, global_cookie_jar
from safe_url.normalized_url import NormalizedUrl


def declared_length_exceeds(header_value: str | None, limit: int) -> bool:
    """True iff a Content-Length header parses to an int strictly over ``limit``;
    malformed/negative/absent reads as unknown size, False (streamed cap still bounds)."""
    if not header_value:
        return False
    try:
        declared = int(header_value)
    except ValueError:
        return False
    return declared > limit


MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


async def redirect_target(
    current_url: NormalizedUrl,
    request_headers: dict[str, str],
    status_code: int,
    location: str | None,
    hops: int,
) -> tuple[NormalizedUrl, dict[str, str]] | None:
    """Decide the next hop of a manual redirect loop (shared by both fetchers).

    On an in-budget redirect returns ``(new_url, updated_headers)``; hop exhaustion
    raises rather than reading the redirect body as a page. Awaited.
    """
    if status_code in _REDIRECT_STATUSES and hops >= MAX_REDIRECT_HOPS:
        # Distinguish hop exhaustion from a terminal answer: the response is still
        # a redirect, so a fetcher must not read its body as rendered content.
        message = f"redirect hop budget exhausted at {current_url.raw}"
        raise httpx.TooManyRedirects(message)
    if status_code not in _REDIRECT_STATUSES or not location:
        return None
    new_url = current_url.resolve(location)
    if new_url is None:
        # ``resolve`` is quiet because discovery drops refused links; a redirect
        # walk refuses loudly, so a refused hop never reads as page success.
        raise ValueError(f"redirect target refused admission: {location!r}")
    if new_url.crosses_origin(current_url):
        request_headers = await _stripped_credentials(request_headers, new_url)
    return new_url, request_headers


async def _stripped_credentials(headers: dict[str, str], new_url: NormalizedUrl) -> dict[str, str]:
    """Drop caller credentials, then attach the new origin's jar cookies.

    Only service-owned identity headers cross an origin; a caller name such as
    ``X-Api-Key`` is dropped, and the jar read is awaited off the loop.
    """
    stripped = {k: v for k, v in headers.items() if survives_cross_origin(k)}
    jar_cookie = await global_cookie_jar().acookie_header_for(new_url, namespace=COOKIE_NAMESPACE.get())
    if jar_cookie:
        stripped["Cookie"] = jar_cookie
    return stripped
