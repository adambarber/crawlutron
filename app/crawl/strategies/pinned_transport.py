"""Owns SSRF validation, DNS resolution, pinning, and redirect targeting.

This module owns transport safety decisions and the pinned request primitives.
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
from urllib.parse import urljoin, urlparse

from app.bounded_ttl import BoundedTTLCache
from app.crawl.credentials import origin_of
from app.models.host_policy import HostPolicy, allow_private_targets
from app.models.http_fetch_result import HttpFetchResult
from app.models.normalized_url import NormalizedUrl
from app.validation import is_public_ip_value


def _is_private_or_loopback_ip(ip: str) -> bool:
    """Return whether an address is outside the shared public-target policy."""
    return not is_public_ip_value(ip)


# Per-host DNS resolution cache. Crawling a single host thousands of times
# would otherwise issue a blocking socket.getaddrinfo per fetch. The TTL is
# deliberately short so DNS changes (and the rebinding window for SSRF) stay
# bounded; the SSRF path re-validates resolved IPs on every fresh resolution.
#
# One locked, bounded, TTL'd store (app.bounded_ttl): the whole-cache clear
# beyond the cap and the manual lock both lived here before, and both
# produced races. LRU eviction inside put() replaces the soft-cap clear.
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
    # The sockaddr's address slot is typed ``str | int | None`` (AF_INET6
    # tuples carry a scope id in a later position, and the socket stub is
    # intentionally loose), but a resolved address is always a string here;
    # coerce per entry so the declared ``list[str]`` contract the SSRF check
    # and the cache rely on is what the function actually delivers.
    ips = [str(entry[4][0]) for entry in addrinfo]
    if use_cache:
        # Only the validation path populates the cache. The pinning path
        # resolves fresh and must never persist an IP here -- otherwise a
        # private/rebound IP seen during pinning would poison the shared
        # validation cache and let the check later treat it as safe.
        _DNS_CACHE.put(hostname, ips)
    return ips


def check_ssrf_protection(url: str) -> None:
    """Resolve the URL hostname and abort if any IP is private/loopback.

    Set ``CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1`` (or ``true``/``yes``/``on``)
    to disable for local testing.
    """
    if allow_private_targets():
        return
    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return
    try:
        ips = _resolve_ips(hostname)
    except socket.gaierror:
        # An unresolvable hostname is not a valid public target; fail closed
        # rather than silently allowing it. Previously this let unresolvable
        # hostnames bypass the private-IP block and reach loopback via the pin
        # fallback in resolve_and_pin_host.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    for ip in ips:
        if not is_public_ip_value(ip):
            raise ValueError(
                f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}"
            ) from None


def resolve_and_pin_host(url: str) -> tuple[str, str]:
    """Resolve the hostname once and pin the resulting IP for the connection.

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

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return hostname or "", hostname or ""
    if allow_private_targets():
        # Local-dev escape hatch: bypass resolution but connect to the REAL
        # hostname. Previously this hardcoded 127.0.0.1, which misrouted every
        # fetch/webhook to loopback and broke virtual-host routing.
        return hostname, hostname
    try:
        # Resolve FRESH (no cache): the pinned IP governs the actual connection
        # and must be re-validated against the SSRF blocklist on every call so a
        # DNS rebind inside the cache TTL cannot bypass the check (TOCTOU).
        ips = _resolve_ips(hostname, use_cache=False)
    except socket.gaierror:
        # Fail closed. Returning the hostname here let the client resolve it again
        # at connect time with no validation applied, which is the open door
        # check_ssrf_protection closes on the other side of this same lookup.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    for ip in ips:
        if is_public_ip_value(ip):
            return hostname, ip
    # No public IP found (should have been caught above) -> fail safe
    raise ValueError(f"SSRF protection: hostname {hostname} has no public IP")


def _declared_length_exceeds(header_value: str | None, limit: int) -> bool:
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


def build_pinned_request(
    url: str,
    headers: dict[str, str],
    *,
    policy: HostPolicy,
) -> tuple[str, dict[str, str]]:
    """Resolve ``url`` once and build the pinned request for it.

    Delegates entirely to the SafeUrl chokepoint: ``parse(pin=True)`` runs
    normalize + SSRF check + resolve-and-pin, and ``connect()`` applies the
    documented pin policy (http -> IP literal + Host header, https ->
    hostname kept for SNI). Keeping the composition in one place is what
    makes IPv6 bracketing, port handling, and the Host header impossible
    to drift between fetch paths.
    """
    from app.safe_url import SafeUrl

    safe = SafeUrl.parse(url, policy=policy, pin=True)
    connect_url, connect_headers = safe.connect()
    return connect_url, {**headers, **connect_headers}


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

    def __call__(
        self, url: str, headers: dict[str, str], *, policy: HostPolicy
    ) -> tuple[str, dict[str, str]]: ...


# Dedicated thread pool for blocking DNS resolution so hundreds of concurrent
# fetches never block the event loop or contend with the default asyncio executor
# (used for extraction and other blocking work). The pinning path resolves FRESH
# on every call for SSRF safety, so without this the loop would stall on each
# resolver call.
DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="dns-resolve")


def _off_loop[**P, R](resolve: Callable[[], Callable[P, R]]) -> Callable[P, Awaitable[R]]:
    """The one way to run blocking call machinery off the event loop.

    Two things are owned here instead of at the call sites: the *pool* and the
    *argument list*. The pool is this module's ``DNS_EXECUTOR``, so every
    off-loop resolution in the process lands on the same 32 workers and the
    default executor stays free for the blocking extraction work it is sized
    for. The argument list is the builder's, carried whole by the ParamSpec
    rather than restated: a wrapper that spelled ``(url, headers, policy)`` next
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


# The fetch path's off-loop entry to the pin rule -- SSRF-safe request building,
# including the blocking ``socket.getaddrinfo`` call, on a worker thread so the
# event loop is never stalled by a resolver lookup.
#
# DERIVED from the rule rather than hand-written beside it. :func:`_off_loop`
# keeps the builder's whole parameter list, so this binding has no signature of
# its own to spell, and an off-loop pinned request is constructible exactly one
# way: on this module's pool, with the arguments :func:`build_pinned_request`
# declares. It used to be a second ``async def`` that restated that list beside
# the builder -- two spellings of the pin rule that were free to disagree, and
# the one rule in this module a caller could get subtly wrong without any test
# noticing.
build_pinned_request_async = _off_loop(lambda: build_pinned_request)


_MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


# Headers that must never survive a cross-host redirect hop.
_CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})


def _redirect_target(
    current_url: str,
    request_headers: dict[str, str],
    status_code: int,
    location: str | None,
    hops: int,
    *,
    policy: HostPolicy,
) -> tuple[str, dict[str, str]] | None:
    """Decide the next hop of a manual redirect loop (shared by both fetchers).

    Returns ``(new_url, updated_headers)`` when the response is an in-budget
    redirect, else ``None`` (terminal response). The caller must re-run
    ``build_pinned_request`` for ``new_url`` so every hop re-validates against
    the SSRF blocklist and re-pins DNS. Sensitive headers (Cookie,
    Authorization) are stripped when crossing an origin boundary.

    The boundary is ``origin_of``, the same helper ``credentials`` scopes by, and
    that sharing is the point. This used to compare ``hostname``, so a hop to a
    different port on the same host kept the credential while ``apply_credentials``
    would have refused to send it there -- one definition of origin was in the
    sibling chokepoint and a looser one here, and the looser one is where the
    credential leaks. Comparing origins also catches an https-to-http downgrade,
    which the host comparison missed in the direction where it matters most.
    """
    if status_code not in _REDIRECT_STATUSES or not location or hops >= _MAX_REDIRECT_HOPS:
        return None
    new_url = str(NormalizedUrl(urljoin(current_url, location), policy=policy))
    if _crosses_origin(new_url, current_url):
        request_headers = {k: v for k, v in request_headers.items() if k.lower() not in _CREDENTIAL_HEADERS}
    return new_url, request_headers


def _crosses_origin(new_url: str, current_url: str) -> bool:
    """Whether a redirect hop leaves the origin, per ``credentials.origin_of``.

    Falls back to treating the hop as a boundary when either side cannot be
    canonicalised. Admission has already rejected the non-HTTP schemes and
    userinfo that ``origin_of`` refuses, so this is defensive -- and it fails toward stripping, since a header we keep is
    unrecoverable and a header we drop only costs a re-login.
    """
    try:
        return origin_of(new_url) != origin_of(current_url)
    except ValueError:  # pragma: no cover - admission precedes this
        return True


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

    __slots__ = ("_off_loop", "hops", "headers", "policy", "url")

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        policy: HostPolicy,
        pin: PinRule,
    ) -> None:
        self.url = url
        # Copied, not aliased: a cross-host hop strips credentials by rebuilding
        # the mapping, and the caller's dict must not be rewritten underneath it.
        self.headers = dict(headers)
        self.policy = policy
        self.hops = 0
        # Off-loading belongs to the call site, not to the rule: a blocking rule
        # goes to the pool whether it is this module's builder or a substituted
        # one, so "pinning never stalls the loop" is a property the walk
        # enforces rather than a property of which name happens to be bound.
        # The rule arrives already resolved: ``None`` means "the real one" is a
        # question :func:`apinned_hops` answers once, and a chain that held the
        # sentinel would be re-deciding it on every construction.
        self._off_loop = _off_loop(lambda: pin)

    async def pin(self) -> tuple[str, dict[str, str]]:
        """The connect-ready request for the current hop (DNS off the loop)."""
        return await self._off_loop(self.url, self.headers, policy=self.policy)

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
            policy=self.policy,
        )
        if target is None:
            return False
        self.url, self.headers = target
        self.hops += 1
        return True


async def apinned_hops(
    url: str,
    headers: dict[str, str],
    *,
    policy: HostPolicy,
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
    chain = _RedirectChain(url, headers, policy=policy, pin=pin or build_pinned_request)
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
