"""Owns SSRF validation, DNS resolution, pinning, and redirect targeting.

Carries the safety decisions and pinned primitives (IP blocklist, fresh pin,
plain-HTTP/HTTPS connect composition); never retries, extracts, or constructs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

import httpx

from app.models.host_policy import allow_private_targets
from app.models.normalized_url import NormalizedUrl
from app.safety.bounded_ttl import BoundedTTLCache
from app.safety.validation import is_public_ip_value


def is_private_or_loopback_ip(ip: str) -> bool:
    """Return whether an address is outside the shared public-target policy."""
    return not is_public_ip_value(ip)


# Per-host DNS cache; the TTL is short so rebinds stay bounded, with IP
# re-validation on every fresh resolution. One locked store: app.safety.bounded_ttl.
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 4096
_DNS_CACHE: BoundedTTLCache[str, list[str]] = BoundedTTLCache(max_entries=_DNS_CACHE_MAX, ttl=_DNS_TTL)


def _resolve_ips(hostname: str, *, use_cache: bool = True) -> list[str]:
    """Resolve a hostname to its IPs on a short-TTL cache (raw addrinfo list,
    IPv6 included; socket.gaierror on failure). The pinning path passes
    use_cache=False so a cached IP never bypasses SSRF via rebind (TOCTOU).
    """
    if use_cache:
        cached = _DNS_CACHE.get(hostname)
        if cached is not None:
            return cached
    addrinfo = socket.getaddrinfo(hostname, None)
    # The sockaddr slot is ``str | int | None`` (AF_INET6 scope id), but a resolved
    # address is always a string here; coerce so the list[str] contract holds.
    ips = [str(entry[4][0]) for entry in addrinfo]
    if use_cache:
        # Only the validation path populates the cache; the pinning path resolves
        # fresh, or a rebound private IP would poison the shared validation cache.
        _DNS_CACHE.put(hostname, ips)
    return ips


def check_ssrf_protection(url: NormalizedUrl) -> None:
    """Resolve the URL's host and abort if any IP is private/loopback. The host
    is read off the admitted value (nothing left to re-parse). Set
    "CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1" (or "true"/"yes"/"on") to disable.
    """
    if allow_private_targets():
        return
    hostname = url.host
    try:
        ips = _resolve_ips(hostname)
    except socket.gaierror:
        # An unresolvable hostname is not a valid public target: fail closed rather
        # than let it reach loopback via the pin fallback.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    for ip in ips:
        if not is_public_ip_value(ip):
            raise ValueError(
                f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}"
            ) from None


def resolve_and_pin_host(url: NormalizedUrl) -> tuple[str, str]:
    """Resolve the host once and pin the resulting IP, connecting to that exact IP
    to defeat DNS-rebinding TOCTOU SSRF. Fails closed: an unresolvable host
    raises instead of falling back to "(hostname, hostname)"; the caller keeps the Host header.
    """

    hostname = url.host
    if allow_private_targets():
        # Local-dev escape hatch: bypass resolution, connect to the real hostname.
        return hostname, hostname
    try:
        # Resolve FRESH (no cache): the pin governs the connection, so it is
        # re-validated every call and a rebind inside the TTL cannot bypass it.
        ips = _resolve_ips(hostname, use_cache=False)
    except socket.gaierror:
        # Fail closed: returning the hostname lets the client re-resolve at connect
        # time with no validation applied.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    for ip in ips:
        if is_public_ip_value(ip):
            return hostname, ip
    # No public IP found (should have been caught above) -> fail safe
    raise ValueError(f"SSRF protection: hostname {hostname} has no public IP")


def declared_length_exceeds(header_value: str | None, limit: int) -> bool:
    """True iff a Content-Length header parses to an int strictly over ``limit``.

    A malformed, negative, or absent value reads as "unknown size" and returns
    False (never raises); skipping pre-flight is safe as the streamed running cap still bounds the body.
    """
    if not header_value:
        return False
    try:
        declared = int(header_value)
    except ValueError:
        return False
    return declared > limit


def build_pinned_request(url: NormalizedUrl, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Build the pinned request for an admitted URL, in one step -- the sole owner
    of the composition (IP blocklist, fresh pin, exact-IP plain-HTTP pin with
    Host header preserved; HTTPS keeps the hostname for TLS SNI). Always pins; one place prevents drift.
    """
    check_ssrf_protection(url)
    if url.scheme == "https":
        return url.raw, dict(headers)
    _, pinned = resolve_and_pin_host(url)
    port = f":{url.port}" if url.port else ""
    # An IPv6 literal must be bracketed in a URL netloc or its colons
    # corrupt port/path parsing; getaddrinfo can return v6.
    host = f"[{pinned}]" if ":" in pinned else pinned
    connect_url = f"{url.scheme}://{host}{port}{url.path or '/'}"
    if url.query:
        connect_url = f"{connect_url}?{url.query}"
    out_headers = dict(headers)
    out_headers["Host"] = f"{url.host}{port}"
    return connect_url, out_headers


class PinRule(Protocol):
    """The rule that turns a URL into a request safe to connect to; named and injected.
    It is the rule, not the resolver, that travels -- the resolver is the
    SSRF decision and must never be doubled; tests inject a pinned request without rebinding the builder.
    """

    def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> tuple[str, dict[str, str]]: ...


# Dedicated thread pool for blocking DNS: concurrent fetches never stall the
# event loop or contend with the default executor used for extraction.
DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="dns-resolve")


def _off_loop[**P, R](resolve: Callable[[], Callable[P, R]]) -> Callable[P, Awaitable[R]]:
    """The one way to run blocking call machinery off the loop: it owns the
    pool (DNS_EXECUTOR) and the builder's argument list, carried whole via the
    ParamSpec so the wrapper cannot disagree. ``resolve`` is a thunk -- the rule is unknown at wrap time.
    """

    async def _run(*args: P.args, **kwargs: P.kwargs) -> R:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(DNS_EXECUTOR, functools.partial(resolve(), *args, **kwargs))

    return functools.wraps(resolve())(_run)


# Off-loop entry to the pin rule -- SSRF-safe build, getaddrinfo included -- on a
# worker thread, so a resolver lookup never stalls the event loop.
build_pinned_request_async = _off_loop(lambda: build_pinned_request)


MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


# Headers that must never survive a cross-host redirect hop.
_CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})


def redirect_target(
    current_url: NormalizedUrl,
    request_headers: dict[str, str],
    status_code: int,
    location: str | None,
    hops: int,
) -> tuple[NormalizedUrl, dict[str, str]] | None:
    """Decide the next hop of a manual redirect loop (shared by both fetchers).

    On an in-budget redirect returns ``(new_url, updated_headers)``; hop exhaustion
    raises rather than reading the redirect body as a page. Each hop re-pins.
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
    if _crosses_origin(new_url, current_url):
        request_headers = {k: v for k, v in request_headers.items() if k.lower() not in _CREDENTIAL_HEADERS}
    return new_url, request_headers


def _crosses_origin(new_url: NormalizedUrl, current_url: NormalizedUrl) -> bool:
    """Whether a redirect hop leaves the origin (the scheme/host/port law shared
    with ``credentials.origin_of``). The direct comparison is the fail-safe reading,
    failing toward stripping: a kept header is unrecoverable; a dropped one only costs a re-login.
    """
    return new_url.origin != current_url.origin


class _RedirectChain:
    """One hop-by-hop redirect walk: its state, and the decision that advances it.

    Both transports follow redirects by hand (automatic would connect to an
    un-revalidated target); the state each carried as locals is one value here.
    """

    __slots__ = ("_off_loop", "hops", "headers", "url")

    def __init__(self, url: NormalizedUrl, headers: dict[str, str], *, pin: PinRule) -> None:
        self.url = url
        # Copied, not aliased: a cross-host hop strips credentials by rebuilding
        # the mapping, and the caller's dict must not be rewritten underneath it.
        self.headers = dict(headers)
        self.hops = 0
        # Off-loading belongs to the call site, not the rule (a substituted builder
        # goes to the pool too); ``None`` resolves the real rule once, per chain.
        self._off_loop = _off_loop(lambda: pin)

    async def pin(self) -> tuple[str, dict[str, str]]:
        """The connect-ready request for the current hop (DNS off the loop)."""
        return await self._off_loop(self.url, self.headers)

    def advance(self, response: Any) -> bool:
        """Step to the next hop; return True when the caller should re-request.

        False means terminal - read the body; the next hop re-runs :meth:`pin`,
        re-validating SSRF. It takes the response so the redirect read is part of the hop decision.
        """
        target = redirect_target(
            self.url,
            self.headers,
            response.status_code,
            response.headers.get("location"),
            self.hops,
        )
        if target is None:
            return False
        self.url, self.headers = target
        self.hops += 1
        return True


async def apinned_hops(
    url: NormalizedUrl,
    headers: dict[str, str],
    *,
    pin: PinRule | None = None,
) -> AsyncIterator[tuple[_RedirectChain, str, dict[str, str]]]:
    """Yield each hop of the manual redirect walk, already SSRF-checked and re-pinned.

    The body receives ``(chain, pinned_url, hop_headers)``, sends, then leaves;
    ``continue`` after ``chain.advance(response)`` asks for the next hop.
    """
    chain = _RedirectChain(url, headers, pin=pin or build_pinned_request)
    while True:
        pinned_url, hop_headers = await chain.pin()
        served_hops = chain.hops
        yield chain, pinned_url, hop_headers
        if chain.hops == served_hops:
            raise AssertionError(
                "the walk body ended without returning a result and without "
                "chain.advance(response) asking for another hop"
            )


def reset_process_state() -> None:
    """Drop every memoized DNS resolution.

    The cache is process-wide with a 60s TTL, so a test that resolves once is
    served the same answer all session; production wants the memo, so the drop lives on the owner.
    """
    _DNS_CACHE.clear()


def has_process_state() -> bool:
    """Whether any hostname's resolution is memoized right now."""
    return bool(_DNS_CACHE)
