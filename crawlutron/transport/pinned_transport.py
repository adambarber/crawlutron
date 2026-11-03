"""Owns SSRF validation, DNS resolution, pinning, and redirect targeting.

Carries the safety decisions and pinned primitives (IP blocklist, fresh pin,
plain-HTTP/HTTPS connect composition); never retries, extracts, or constructs.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
import typing
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from crawlutron.models.host_policy import allow_private_targets
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.bounded_ttl import BoundedTTLCache
from crawlutron.safety.header_values import reject_injectable_header_values
from crawlutron.safety.validation import is_public_ip_value
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE, global_cookie_jar


def is_private_or_loopback_ip(ip: str) -> bool:
    """Return whether an address is outside the shared public-target policy."""
    return not is_public_ip_value(ip)


class ResolverBudgetExceeded(TimeoutError):
    """A DNS resolve outlasted the budget derived from the request it serves:
    classifiable as a resolution failure (dns_error), never as a
    navigation one -- the request never got to navigate."""


async def bounded_thread_resolve[T](work: Callable[[], T], *, timeout_seconds: float | None) -> T:
    """Run one blocking resolve off the loop, bounded by the DNS posture tightened
    by the caller's own budget; a resolver that outlasts the wait raises the
    classifiable failure. Registered in the SAME saturation registry as the sync
    path (#C10): the chokepoint covers every DNS entry point, not one of two."""
    wait_for = dns_wait_for(timeout_seconds)
    _refuse_if_saturated()
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(DNS_EXECUTOR, work)
    _dns_futures.add(future)
    try:
        return await asyncio.wait_for(future, timeout=wait_for)
    except TimeoutError:
        future.cancel()
        raise ResolverBudgetExceeded(f"DNS resolution exceeded the {wait_for:.1f}s budget") from None
    finally:
        _dns_futures.discard(future)


# Per-host DNS cache; the TTL is short so rebinds stay bounded, with IP
# re-validation on every fresh resolution. One locked store: crawlutron.safety.bounded_ttl.
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 4096
_DNS_CACHE: BoundedTTLCache[str, list[str]] = BoundedTTLCache(max_entries=_DNS_CACHE_MAX, ttl=_DNS_TTL)


def _refuse_if_saturated() -> None:
    """The saturation law, ONE copy (#C10): all workers inside live resolves =>
    a new resolve refuses FAST instead of queueing behind uncancellable threads."""
    if len(_dns_futures) >= DNS_WORKERS:
        raise ResolverBudgetExceeded(
            f"DNS resolver saturated ({DNS_WORKERS} workers busy); refusing to queue"
        )


def bounded_addrinfo(hostname: str) -> list[str]:
    """One getaddrinfo on the shared DNS executor, bounded by the DNS posture: a
    resolve that outlasts the budget raises the classifiable ResolverBudgetExceeded
    instead of waiting unbounded on the caller's thread. Saturated => refuse fast (#C10)."""
    wait_for = dns_wait_for(None)
    _refuse_if_saturated()
    future = DNS_EXECUTOR.submit(socket.getaddrinfo, hostname, None)
    _dns_futures.add(future)
    try:
        done, _ = concurrent.futures.wait({future}, timeout=wait_for)
    finally:
        _dns_futures.discard(future)
    if not done:
        future.cancel()
        raise ResolverBudgetExceeded(f"DNS resolution exceeded the {wait_for:.1f}s budget") from None
    # The sockaddr slot is ``str | int | None`` (AF_INET6 scope id), but a resolved
    # address is always a string here; coerce so the list[str] contract holds.
    return [str(entry[4][0]) for entry in future.result()]


def _resolve_ips(hostname: str, *, use_cache: bool = True) -> list[str]:
    """Resolve a hostname to its IPs on a short-TTL cache (raw addrinfo list,
    IPv6 included; socket.gaierror on failure). The pinning path passes
    use_cache=False so a cached IP never bypasses SSRF via rebind (TOCTOU).
    """
    if use_cache:
        cached = _DNS_CACHE.get(hostname)
        if cached is not None:
            return cached
    ips = bounded_addrinfo(hostname)
    if use_cache:
        # Only the validation path populates the cache; the pinning path resolves
        # fresh, or a rebound private IP would poison the shared validation cache.
        _DNS_CACHE.put(hostname, ips)
    return ips


# Pass ``use_cache=False`` where the caller cannot pin its connect (the browser
# re-resolves at connect), so validation is contemporaneous with that connect.
def _public_verdict(hostname: str, ips: list[str]) -> None:
    """The one public-only law over a resolved answer: any private member
    raises the shared refusal. Both check verdicts and the pin read this."""
    for ip in ips:
        if not is_public_ip_value(ip):
            raise ValueError(
                f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}"
            ) from None


def _resolve_or_refuse(hostname: str, *, use_cache: bool) -> list[str]:
    """One resolve (cached or fresh) with the shared fail-closed fault law."""
    try:
        return _resolve_ips(hostname, use_cache=use_cache)
    except (socket.gaierror, socket.herror, OSError, TimeoutError):
        # Any OS resolver fault (gaierror, herror, a degraded resolver's timeout/OSError)
        # is not a valid public target: fail closed, never reach loopback via the pin.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None


def _validated_fresh_answer(url: NormalizedUrl) -> set[str]:
    """One fresh (uncached) resolve, asserted all-public: the shared verdict body
    of ``check_ssrf_protection`` (one resolve) and ``check_ssrf_protection_stable``
    (two, compared)."""
    ips = _resolve_or_refuse(url.host, use_cache=False)
    _public_verdict(url.host, ips)
    return set(ips)


def check_ssrf_protection(url: NormalizedUrl, *, use_cache: bool = True) -> None:
    """Resolve the URL's host and abort if any IP is private/loopback. The host
    is read off the admitted value (nothing left to re-parse). Set
    "CRAWLUTRON_ALLOW_PRIVATE_TARGETS=1" (or "true"/"yes"/"on") to disable.
    """
    if allow_private_targets():
        return
    _public_verdict(url.host, _resolve_or_refuse(url.host, use_cache=use_cache))


def check_ssrf_protection_stable(url: NormalizedUrl) -> None:
    """The browser-family verdict: TWO fresh resolves must agree.

    Last verifiable moment without dial ownership: a changed answer is the
    rebinding signature (residual documented on the ticket).
    """
    if allow_private_targets():
        return
    first = _validated_fresh_answer(url)
    second = _validated_fresh_answer(url)
    if first != second:
        raise ValueError(
            f"SSRF protection: hostname {url.host} DNS answer changed between fresh "
            f"resolves ({sorted(first)} -> {sorted(second)}); refusing an unstable target"
        )


def resolve_and_pin_host(url: NormalizedUrl) -> tuple[str, str]:
    """Resolve the host once and pin the resulting IP, connecting to that exact IP
    to defeat DNS-rebinding TOCTOU SSRF. Fails closed; ONE verdict per hostname --
    a mixed public/private answer set is refused, matching the browser ruling."""

    hostname = url.host
    if allow_private_targets():
        # Local-dev escape hatch: bypass resolution, connect to the real hostname.
        return hostname, hostname
    try:
        # Resolve FRESH (no cache): the pin governs the connection, so it is
        # re-validated every call and a rebind inside the TTL cannot bypass it.
        ips = _resolve_ips(hostname, use_cache=False)
    except (socket.gaierror, socket.herror, OSError, TimeoutError):
        # Fail closed against every OS resolver fault: returning the hostname would
        # let the client re-resolve at connect time with no validation applied.
        raise ValueError(f"SSRF protection: cannot resolve hostname {hostname}") from None
    if not ips:
        raise ValueError(f"SSRF protection: hostname {hostname} has no public IP")
    for ip in ips:
        if not is_public_ip_value(ip):
            raise ValueError(
                f"SSRF protection: hostname {hostname} resolves to private/loopback IP {ip}"
            ) from None
    return hostname, ips[0]


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
    _, pinned = resolve_and_pin_host(url)
    port = f":{url.port}" if url.port else ""
    # An IPv6 literal must be bracketed in a URL netloc or its colons
    # corrupt port/path parsing; getaddrinfo can return v6.
    host = f"[{pinned}]" if ":" in pinned else pinned
    connect_url = f"{url.scheme}://{host}{port}{url.path or '/'}"
    if url.query:
        connect_url = f"{connect_url}?{url.query}"
    out_headers = dict(headers)
    out_headers["Host"] = f"[{url.host}]{port}" if ":" in url.host else f"{url.host}{port}"
    return PinnedRequest(connect_url=connect_url, headers=out_headers, url=url)


def apinned_stream(
    client: httpx.AsyncClient,
    chain: _RedirectChain,
    request: PinnedRequest,
    timeout: httpx.Timeout,
) -> typing.AsyncContextManager[httpx.Response]:
    """Open the one SSRF-pinned httpx stream for a hop: GET to the validated IP
    with the logical hostname carried as TLS SNI and redirects left to the chain."""
    return client.stream(
        "GET",
        request.connect_url,
        headers=request.headers,
        timeout=timeout,
        follow_redirects=False,
        extensions={"sni_hostname": sni} if (sni := request.tls_hostname) else None,
    )


class PinRule(Protocol):
    """The rule that turns a URL into a request safe to connect to; named and injected.
    It is the rule, not the resolver, that travels -- the resolver is the
    SSRF decision and must never be doubled; tests inject a pinned request without rebinding the builder.
    """

    def __call__(self, url: NormalizedUrl, headers: dict[str, str]) -> PinnedRequest: ...


# Dedicated thread pool for blocking DNS: concurrent fetches never stall the
# event loop or contend with the default executor used for extraction.
DNS_WORKERS = 32
DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=DNS_WORKERS, thread_name_prefix="dns-resolve"
)

# A RUNNING getaddrinfo cannot be cancelled, so a hostile/slow resolver can
# occupy every worker; queued resolves then each wait the full budget -- the
# request path hostage to a queue (#C10). Saturation law: when every worker is
# inside a live resolve, a new resolve refuses FAST instead of queueing. Both
# DNS entry points register here (sync concurrent + async asyncio-wrapped,
# pass 5); the registry is event-loop-side only, so it needs no lock.
_DnsFuture = concurrent.futures.Future | asyncio.Future
_dns_futures: set[_DnsFuture] = set()


def dns_in_flight_resolves() -> int:
    """Public door over the saturation accounting: live resolver workers now."""
    return sum(1 for f in _dns_futures if not f.done())


def dns_seed_saturation(workers: int) -> None:
    """TEST DOOR: seed the registry to full capacity so the saturation verdict
    is provable without real network resolves. Pairs with dns_clear_saturation."""
    _dns_futures.update({concurrent.futures.Future() for _ in range(workers)})


def dns_clear_saturation() -> None:
    """TEST DOOR: drop the seeded placeholders -- a never-started Future cancels
    (True); a real in-flight resolve does not (False) and stays accounted."""
    for future in list(_dns_futures):
        if future.cancel():
            _dns_futures.discard(future)


# The DNS wait posture: the ceiling on the resolver wait, derived from
# the request's budget by ``dns_wait_for`` so a tight request tightens it.
DNS_WAIT_SECONDS = 5.0


def dns_wait_for(timeout_seconds: float | None) -> float:
    """The resolver wait for a request with ``timeout_seconds`` of budget:
    the request's own budget when it is the tighter one, else the DNS posture."""
    if timeout_seconds is None:
        return DNS_WAIT_SECONDS
    return min(max(0.1, float(timeout_seconds)), DNS_WAIT_SECONDS)


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
    return await bounded_thread_resolve(
        lambda: build_pinned_request(url, headers), timeout_seconds=timeout_seconds
    )


MAX_REDIRECT_HOPS = 5

_REDIRECT_STATUSES = frozenset((301, 302, 303, 307, 308))


# Headers that must never survive a cross-host redirect hop.
_CREDENTIAL_HEADERS = frozenset({"cookie", "authorization", "proxy-authorization"})


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
    """Drop caller credentials, then attach the new origin's own jar cookies.

    Caller-supplied credentials never cross origins; the jar read is SQLite
    under the store lock, so this is awaited, never on the loop.
    """
    stripped = {k: v for k, v in headers.items() if k.lower() not in _CREDENTIAL_HEADERS}
    jar_cookie = await global_cookie_jar().acookie_header_for(new_url, namespace=COOKIE_NAMESPACE.get())
    if jar_cookie:
        stripped["Cookie"] = jar_cookie
    return stripped


class _RedirectChain:
    """One hop-by-hop redirect walk: its state, and the decision that advances it.

    Both transports follow redirects by hand (automatic would connect to an
    un-revalidated target); the state each carried as locals is one value here.
    """

    __slots__ = ("_rule", "hops", "headers", "timeout_seconds", "url")

    def __init__(
        self,
        url: NormalizedUrl,
        headers: dict[str, str],
        *,
        pin: PinRule,
        timeout_seconds: float | None = None,
    ) -> None:
        self.url = url
        # Copied, not aliased: a cross-host hop strips credentials by rebuilding
        # the mapping, and the caller's dict must not be rewritten underneath it.
        self.headers = dict(headers)
        self.hops = 0
        # The request's own budget bounds the resolver wait: a slow
        # getaddrinfo surfaces a classifiable timeout, not an indefinite hang.
        self.timeout_seconds = timeout_seconds
        # The rule travels whole; the bounded off-loop resolve wraps it at pin
        # time, so a substituted builder goes to the pool with the same bound.
        self._rule = pin

    async def pin(self) -> PinnedRequest:
        """The connect-ready request for the current hop (DNS off the loop, wait
        bounded by the request's own budget)."""
        return await bounded_thread_resolve(
            lambda: self._rule(self.url, self.headers), timeout_seconds=self.timeout_seconds
        )

    async def advance(self, response: httpx.Response) -> NormalizedUrl | None:
        """Step to the next hop; return the URL the response was served from.

        ``None`` means terminal - read the body. The served URL is captured
        BEFORE the step: a redirect response's cookie state belongs to the
        origin that sent it (#571), and this is the one decision site -- the
        return value is the only post-advance handle a caller gets, so a
        read-after-mutate of ``self.url`` is unrepresentable. Awaited: a
        cross-origin hop reads the persistent jar off the loop.
        """
        served_url = self.url
        target = await redirect_target(
            self.url,
            self.headers,
            response.status_code,
            response.headers.get("location"),
            self.hops,
        )
        if target is None:
            return None
        self.url, self.headers = target
        self.hops += 1
        return served_url


async def apinned_hops(
    url: NormalizedUrl,
    headers: dict[str, str],
    *,
    pin: PinRule | None = None,
    timeout_seconds: float | None = None,
) -> AsyncIterator[tuple[_RedirectChain, PinnedRequest]]:
    """Yield each hop of the manual redirect walk, already SSRF-checked and re-pinned.

    The body receives ``(chain, pinned_request)``, sends, then leaves;
    ``continue`` after ``chain.advance(response)`` asks for the next hop.
    """
    # timeout_seconds is the request budget; it bounds the resolver wait.
    # The shared wire edge: a value that can start another header is refused here.
    reject_injectable_header_values(headers)
    chain = _RedirectChain(url, headers, pin=pin or build_pinned_request, timeout_seconds=timeout_seconds)
    while True:
        pinned = await chain.pin()
        # Service-built additions (Host, hop cookies) ride the same law.
        reject_injectable_header_values(pinned.headers)
        served_hops = chain.hops
        yield chain, pinned
        if chain.hops == served_hops:
            raise AssertionError(
                "the walk body ended without returning a result and without "
                "chain.advance(response) asking for another hop"
            )


def cffi_resolve_entry(request: PinnedRequest) -> list[str] | None:
    """The curl_cffi resolve table for a pinned hop: ``host:port:ip`` dialing the
    pinned IP while SNI and cert verification stay on the logical hostname.
    None when there is nothing to pin (HTTP hops, or the local-dev posture where
    the pin is the hostname itself).
    """
    if request.connect_url == request.url.raw:
        return None
    tls = request.tls_hostname
    if tls is None:
        return None
    parsed = urlparse(request.connect_url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return [f"{tls}:{port}:{parsed.hostname}"]


def reset_process_state() -> None:
    """Drop every memoized DNS resolution: the cache is process-wide with a 60s
    TTL, so a test that resolves once is served the same answer all session."""
    _DNS_CACHE.clear()


def has_process_state() -> bool:
    """Whether any hostname's resolution is memoized right now."""
    return bool(_DNS_CACHE)
