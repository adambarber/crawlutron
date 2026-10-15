"""Owns SSRF validation, DNS resolution, pinning, and redirect targeting.

This module owns transport safety decisions and the pinned request
primitives: the resolved-IP blocklist, the fresh-IP pin, and the one
connect composition (pinned IP literal + Host header for plain HTTP,
hostname kept for HTTPS) that every fetching path builds requests
through.

It must not perform retries, response extraction, or result construction.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
import socket
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, Protocol

from app.models.host_policy import allow_private_targets
from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl
from app.safety.bounded_ttl import BoundedTTLCache
from app.safety.validation import is_public_ip_value


def _is_private_or_loopback_ip(ip: str) -> bool:
    """Return whether an address is outside the shared public-target policy."""
    return not is_public_ip_value(ip)


# Per-host DNS cache; the TTL is short so rebinds stay bounded, with IP
# re-validation on every fresh resolution. One locked store: app.safety.bounded_ttl.
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 4096
_DNS_CACHE: BoundedTTLCache[str, list[str]] = BoundedTTLCache(max_entries=_DNS_CACHE_MAX, ttl=_DNS_TTL)


def _resolve_ips(hostname: str, *, use_cache: bool = True) -> list[str]:
    """Resolve a hostname to its IPs with a short TTL cache.

    Returns the raw IP list from socket.getaddrinfo (may include IPv6). Raises
    socket.gaierror on resolution failure.

    Caching avoids a blocking resolver call on every fetch in high-concurrency
    batches. The pinning path (``resolve_and_pin_host``) passes
    ``use_cache=False`` so the connection always pins a *freshly* resolved,
    freshly re-validated public IP -- a cached pin would let a DNS rebind
    inside the TTL window bypass the SSRF check (TOCTOU). The validation-only
    path (``check_ssrf_protection``) may use the cache: it only needs to know
    the host is safe to connect to, and the actual connection re-validates.
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
    """Resolve the URL's host and abort if any IP is private/loopback.

    The host is read off the admitted value; there is nothing left to
    re-parse here. Set ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1`` (or
    ``true``/``yes``/``on``) to disable for local testing.
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
    """Resolve the URL's host once and pin the resulting IP for the connection.

    Prevents DNS-rebinding (TOCTOU) SSRF: ``check_ssrf_protection`` validates
    the resolved IPs, then this helper returns the FIRST public IP so the HTTP
    client connects to that exact IP instead of re-resolving at connect time.
    The original ``Host`` header is preserved by the caller.

    Fails closed. A hostname that cannot be resolved here raises rather than
    falling back to ``(hostname, hostname)``, which read as harmless but is not:
    it hands the HTTP client permission to resolve the name a third time, at
    connect time, with nothing validating the answer. ``check_ssrf_protection``
    checked the IPs it saw and this discarded them, so an attacker able to fail
    the pin-time lookup got an unvalidated connection -- the rebinding this
    function exists to prevent, reached by breaking the pin instead of racing it.
    ``check_ssrf_protection`` already refuses the same condition with the same
    message; this keeps the two halves of one guarantee refusing together.
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
    """True iff a ``Content-Length`` header parses to an int strictly over ``limit``.

    A malformed, negative, or absent value reads as "unknown size" and returns
    False -- it must never raise. Skipping the pre-flight rejection is
    safe: the streamed running cap still bounds the actual body, so a hostile
    ``Content-Length: abc`` cannot crash the fetch or spoof a pass.
    """
    if not header_value:
        return False
    try:
        declared = int(header_value)
    except ValueError:
        return False
    return declared > limit


def _http_413(url: NormalizedUrl, final_url: NormalizedUrl, started: float) -> HttpFetchResult:
    """Standard 413 (payload too large) result for size/cap violations.

    Takes the logical ``final_url`` (never ``current_url``): for a
    pinned plain-HTTP fetch the response url is the resolved-IP literal, and
    surfacing it leaks topology and mis-keys cookies by IP.
    """
    return HttpFetchResult(
        url=url,
        final_url=final_url,
        status_code=413,
        headers={"content-type": "application/octet-stream"},
        body=b"",
        elapsed_ms=int((time.monotonic() - started) * 1000),
    )


def build_pinned_request(url: NormalizedUrl, headers: dict[str, str]) -> tuple[str, dict[str, str]]:
    """Build the pinned request for an admitted URL, in one step.

    This is the one owner of the pin policy's connect composition: the
    resolved-IP blocklist check, the fresh pin, IPv6 bracketing, port
    handling, and the Host header. Plain HTTP connects to the exact
    validated IP (closing the DNS-rebinding TOCTOU between validation and
    connect) with a Host header preserving the original hostname for
    virtual hosting; HTTPS keeps the hostname so TLS SNI / certificate
    verification works (real certs carry no IP SAN) -- there the rebinding
    window is bounded by CA validation plus this pre-connect check.

    The unsafe state the old ``SafeUrl.connect`` refused is now
    unrepresentable rather than refused: this builder always pins, so there
    is no unpinned plain-HTTP branch left to raise from. Keeping the
    composition in one place is what makes it impossible to drift between
    fetch paths -- both HTTP fetchers, both browser transports, and
    webhook delivery all build requests through this one builder.
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
    """The rule that turns a URL into a request safe to connect to.

    Named and injected rather than reached for. A test that wants a pinned
    request without a real resolver used to rebind this module's private
    builder, which tied eight test modules to a name the SSRF chokepoint was
    never meant to expose -- and it made the walk's threading property
    untestable under substitution, because off-loading was welded to the module
    binding rather than to the call site.

    It is the rule and not the resolver that travels, deliberately.
    Substituting the resolver would exercise more of the rule, which is
    better, but the resolver is the one part of this module a double must
    never stand in for: it is the SSRF decision itself, not a collaborator. So
    the seam is placed outside it.
    """

    def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> tuple[str, dict[str, str]]: ...


# Dedicated thread pool for blocking DNS: concurrent fetches never stall the
# event loop or contend with the default executor used for extraction.
DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="dns-resolve")


def _off_loop[**P, R](resolve: Callable[[], Callable[P, R]]) -> Callable[P, Awaitable[R]]:
    """The one way to run blocking call machinery off the event loop.

    Two things are owned here instead of at the call sites: the *pool* and the
    *argument list*. The pool is this module's ``DNS_EXECUTOR``, so every
    off-loop resolution in the process lands on the same 32 workers and the
    default executor stays free for the blocking extraction work it is sized
    for. The argument list is the builder's, carried whole by the ParamSpec
    rather than restated: a wrapper that spelled ``(url, headers)`` next
    to a builder spelling the same list would be a second copy of the pin rule,
    and the copy is free to disagree with the rule it wraps. Here the two cannot
    disagree, so there is nothing left to keep in sync.

    ``resolve`` is a thunk, not the builder, because the rule is not known when
    the wrapper is made: a chain wraps whichever rule it was handed, default or
    substituted, and binding at wrap time would freeze this module's builder into
    every later call.
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


def _redirect_target(
    current_url: NormalizedUrl,
    request_headers: dict[str, str],
    status_code: int,
    location: str | None,
    hops: int,
) -> tuple[NormalizedUrl, dict[str, str]] | None:
    """Decide the next hop of a manual redirect loop (shared by both fetchers).

    Returns ``(new_url, updated_headers)`` when the response is an in-budget
    redirect, else ``None`` (terminal response). The next URL is admitted by
    the value's own ``resolve``, so every hop re-runs the full admission
    policy; the caller then re-runs ``build_pinned_request`` for it so every
    hop re-validates against the SSRF blocklist and re-pins DNS. Sensitive
    headers (Cookie, Authorization) are stripped when crossing an origin
    boundary.

    The origin boundary is the value's ``.origin``, the same scheme/host/port
    law ``credentials.origin_of`` spells for text keys, and that sharing is
    the point. This used to compare ``hostname``, so a hop to a different
    port on the same host kept the credential while ``apply_credentials``
    would have refused to send it there -- one definition of origin was in
    the sibling chokepoint and a looser one here, and the looser one is
    where the credential leaks. Comparing origins also catches an
    https-to-http downgrade, which the host comparison missed in the
    direction where it matters most.
    """
    if status_code not in _REDIRECT_STATUSES or not location or hops >= MAX_REDIRECT_HOPS:
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
    """Whether a redirect hop leaves the origin.

    The origin law lives on the value -- ``NormalizedUrl.origin`` -- the same
    scheme/host/port definition ``credentials.origin_of`` spells for text
    config keys, so the walk and credential scoping cannot disagree. Both
    operands here are admitted values, so there is no unparseable case left
    to fall back from: the direct comparison IS the fail-safe reading, and
    it still fails toward stripping, since a header kept is unrecoverable
    and a header dropped only costs a re-login.
    """
    return new_url.origin != current_url.origin


class _RedirectChain:
    """One hop-by-hop redirect walk: its state, and the decision that advances it.

    Both transports follow redirects by hand -- automatic following would
    connect to a target nobody re-validated -- so each one carried the walk's
    state as three locals (current url, per-hop headers, hop count) and each
    spelled that state back out as six arguments to ``_redirect_target``. The
    state is one value, so it is one value here, and the hop budget and the
    cross-host credential strip are applied by the same code on both paths
    rather than by two call sites that can disagree.
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

        False means this response is terminal and the caller should read its
        body. The next iteration re-runs :meth:`pin`, which re-validates the
        new target against the SSRF blocklist and re-pins its DNS.

        Takes the response rather than ``(status_code, location)`` so that
        reading the redirect off it is part of the hop decision instead of two
        per-transport header lookups that have to agree.
        """
        target = _redirect_target(
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
    """Yield each hop of the manual redirect walk, already SSRF-checked.

    This is the loop both transports used to spell for themselves: pin, send,
    decide, repeat. Only the *send* differs between them -- httpx streams
    through its pooled client's ``stream()``, curl_cffi through a session
    ``get()`` -- so it stays at the call site, while the parts that must not
    differ are here. Every iteration re-pins through :meth:`_RedirectChain.pin`,
    so each redirect target is re-validated against the SSRF blocklist and
    re-resolved to a fresh IP; :meth:`_RedirectChain.advance` applies the
    shared hop budget and the cross-host credential strip. A transport that
    re-spelled the loop could drop either one and nothing would notice.

    The body drives the walk: it receives ``(chain, pinned_url, hop_headers)``,
    sends it, and leaves the block. ``continue`` after
    ``chain.advance(response)`` asked for another hop; every other exit is the
    end of the walk -- the terminal body finished reading, an early 413
    returned, a transport error raised. The hop count is what tells the two
    apart, and it is the count only ``advance`` increments, so a body that
    forgets to advance cannot silently keep the walk going: it ends here.
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

    The cache is process-wide with a 60s TTL, so a test that resolves a host once
    is served the same answer for the rest of the session. Production wants the
    memo -- a single host crawled thousands of times would otherwise pay a blocking
    getaddrinfo per fetch -- which is exactly why the drop belongs here, named, on
    the owner, rather than in every caller that happens to need a clean slate.
    """
    _DNS_CACHE.clear()


def has_process_state() -> bool:
    """Whether any hostname's resolution is memoized right now."""
    return bool(_DNS_CACHE)
