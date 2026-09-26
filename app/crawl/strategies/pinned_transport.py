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
from typing import Any
from urllib.parse import urljoin, urlparse

from app.bounded_ttl import BoundedTTLCache
from app.models import HttpFetchResult
from app.validation import HostPolicy, _is_public_ip_value, normalize_public_http_url


def _is_private_or_loopback_ip(ip: str) -> bool:
    """Return whether an address is outside the shared public-target policy."""
    from app.validation import _is_public_ip_value

    return not _is_public_ip_value(ip)


# Per-host DNS resolution cache (#3). Crawling a single host thousands of times
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
    batches. The pinning path (``_resolve_and_pin_host``) passes
    ``use_cache=False`` so the connection always pins a *freshly* resolved,
    freshly re-validated public IP -- a cached pin would let a DNS rebind
    inside the TTL window bypass the SSRF check (TOCTOU). The validation-only
    path (``_check_ssrf_protection``) may use the cache: it only needs to know
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


def _check_ssrf_protection(url: str) -> None:
    """Resolve the URL hostname and abort if any IP is private/loopback.

    Set ``BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS=1`` (or ``true``/``yes``/``on``)
    to disable for local testing.
    """
    from app.validation import parse_bool_env

    if parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False):
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
        # fallback in _resolve_and_pin_host.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    for ip in ips:
        if not _is_public_ip_value(ip):
            raise ValueError(
                f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}"
            ) from None


def _resolve_and_pin_host(url: str) -> tuple[str, str]:
    """Resolve the hostname once and pin the resulting IP for the connection.

    Prevents DNS-rebinding (TOCTOU) SSRF: ``_check_ssrf_protection`` validates
    the resolved IPs, then this helper returns the FIRST public IP so the HTTP
    client connects to that exact IP instead of re-resolving at connect time.
    The original ``Host`` header is preserved by the caller.
    """

    parsed = urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        return hostname or "", hostname or ""
    from app.validation import parse_bool_env

    if parse_bool_env("BROWSER_CRAWLER_ALLOW_PRIVATE_TARGETS", default=False):
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
        # Do NOT fall back to loopback. Returning the original hostname makes
        # the HTTP client attempt resolution itself and fail with a connection
        # error instead of silently connecting to 127.0.0.1 (SSRF bypass, F9).
        return hostname, hostname
    for ip in ips:
        if _is_public_ip_value(ip):
            return hostname, ip
    # No public IP found (should have been caught above) -> fail safe
    raise ValueError(f"SSRF protection: hostname {hostname} has no public IP")


def _declared_length_exceeds(header_value: str | None, limit: int) -> bool:
    """True iff a ``Content-Length`` header parses to an int strictly over ``limit``.

    A malformed, negative, or absent value reads as "unknown size" and returns
    False (L2) -- it must never raise. Skipping the pre-flight rejection is
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


def _http_413(url: str, final_url: str, started: float) -> HttpFetchResult:
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


def _build_pinned_request(
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
    to drift between fetch paths (H5-C).
    """
    from app.safe_url import SafeUrl

    safe = SafeUrl.parse(url, policy=policy, pin=True)
    connect_url, connect_headers = safe.connect()
    return connect_url, {**headers, **connect_headers}


# Dedicated thread pool for blocking DNS resolution so hundreds of concurrent
# fetches never block the event loop or contend with the default asyncio executor
# (used for extraction and other blocking work). The pinning path resolves FRESH
# on every call for SSRF safety, so without this the loop would stall on each
# resolver call (adversarial perf #1.1).
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="dns-resolve")


async def _build_pinned_request_async(
    url: str,
    headers: dict[str, str],
    *,
    policy: HostPolicy,
) -> tuple[str, dict[str, str]]:
    """Offload SSRF-safe request building (incl. the blocking ``socket.getaddrinfo``
    call) to a worker thread so the event loop is never stalled by a resolver
    lookup (adversarial perf #1.1). Mirrors the browser route handler, which
    already runs ``_build_pinned_request`` via ``asyncio.to_thread``.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _DNS_EXECUTOR,
        functools.partial(_build_pinned_request, url, headers, policy=policy),
    )


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
    ``_build_pinned_request`` for ``new_url`` so every hop re-validates against
    the SSRF blocklist and re-pins DNS (F1). Sensitive headers (Cookie,
    Authorization) are stripped when crossing host boundaries (Fix 3).
    """
    if status_code not in _REDIRECT_STATUSES or not location or hops >= _MAX_REDIRECT_HOPS:
        return None
    new_url = str(normalize_public_http_url(urljoin(current_url, location), policy=policy))
    new_host = urlparse(new_url).hostname
    current_host = urlparse(current_url).hostname
    if new_host != current_host:
        request_headers = {k: v for k, v in request_headers.items() if k.lower() not in _CREDENTIAL_HEADERS}
    return new_url, request_headers


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

    __slots__ = ("hops", "headers", "policy", "url")

    def __init__(self, url: str, headers: dict[str, str], *, policy: HostPolicy) -> None:
        self.url = url
        # Copied, not aliased: a cross-host hop strips credentials by rebuilding
        # the mapping, and the caller's dict must not be rewritten underneath it.
        self.headers = dict(headers)
        self.policy = policy
        self.hops = 0

    async def pin(self) -> tuple[str, dict[str, str]]:
        """The connect-ready request for the current hop (DNS off the loop)."""
        return await _build_pinned_request_async(self.url, self.headers, policy=self.policy)

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
            self.url, self.headers, response.status_code,
            response.headers.get("location"), self.hops, policy=self.policy,
        )
        if target is None:
            return False
        self.url, self.headers = target
        self.hops += 1
        return True
