"""Owns request pinning, redirect targeting, and stream composition.

Carries the pinned primitives over an admitted URL (fresh pin, connect
composition, redirect-chain rules); never retries, extracts, or constructs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from crawlutron.safety.header_authority import survives_cross_origin
from crawlutron.transport import pinned_dns as _dns
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


@dataclass(frozen=True, slots=True)
class PinnedRequest:
    """One hop's wire plan: where to dial and what TLS must attest.

    The composition owns both -- identity travels with the pin, so no
    transport ever re-derives a hostname from a connect URL (#574).
    """

    connect_url: str
    headers: dict[str, str]
    url: NormalizedUrl

    @property
    def tls_hostname(self) -> str | None:
        return self.url.tls_hostname


def build_pinned_request(url: NormalizedUrl, headers: dict[str, str]) -> PinnedRequest:
    """Build the pinned request for an admitted URL -- the sole owner of the
    composition (IP blocklist, fresh pin, exact-IP connect with Host preserved).
    Both schemes pin the connect to the validated IP; one resolution per hop.
    """
    _, pinned = _dns.resolve_and_pin_host(url)
    port = f":{url.port}" if url.port else ""
    # An IPv6 literal must be bracketed in a URL netloc or its colons
    # corrupt port/path parsing; getaddrinfo can return v6.
    host = f"[{pinned}]" if ":" in pinned else pinned
    connect_url = f"{url.scheme}://{host}{port}{url.path or '/'}"
    if url.query:
        connect_url = f"{connect_url}?{url.query}"
    out_headers = dict(headers)
    out_headers["Host"] = f"[{url.host}]{port}" if ":" in url.host else f"{url.host}{port}"
    tls = url.tls_hostname
    if tls is not None:
        # Close the connection: the pool keys this hop by the pinned IP, so two
        # hosts on one IP would share one TLS session; reuse is a perf nicety.
        out_headers["Connection"] = "close"
    return PinnedRequest(connect_url=connect_url, headers=out_headers, url=url)


class PinRule(Protocol):
    """The rule that turns a URL into a request safe to connect to; named and injected.
    It is the rule, not the resolver, that travels -- the resolver is the
    SSRF decision and must never be doubled; tests inject a pinned request without rebinding the builder.
    """

    def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> PinnedRequest: ...


async def build_pinned_request_async(
    url: NormalizedUrl,
    headers: dict[str, str],
    *,
    timeout_seconds: float | None = None,
) -> PinnedRequest:
    """Off-loop entry to the pin rule: SSRF-safe build, getaddrinfo included, on a
    worker thread so a resolver lookup never stalls the event loop.
    """
    # The resolver wait is derived from the request's budget: no budget in
    # hand means the DNS posture; a tighter budget tightens the wait.
    return await _dns.bounded_thread_resolve(
        lambda: build_pinned_request(url, headers), timeout_seconds=timeout_seconds
    )


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


def cffi_resolve_entry(request: PinnedRequest) -> list[str] | None:
    """The curl_cffi resolve table for a pinned hop: ``host:port:ip`` dialing the
    pinned IP while SNI and cert verification stay on the logical hostname. None
    when there is nothing to pin (HTTP hops, or a pin-is-hostname posture)."""
    if request.connect_url == request.url.raw:
        return None
    tls = request.tls_hostname
    if tls is None:
        return None
    parsed = urlparse(request.connect_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return [f"{tls}:{port}:{parsed.hostname}"]
