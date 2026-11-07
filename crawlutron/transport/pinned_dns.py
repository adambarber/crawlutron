"""Owns SSRF validation and DNS resolution.

The safety decisions and pinned primitives over a hostname: public-only IP
check, the fresh pin, the saturation sand the resolver wait budget. Never
builds requests, redirects, or streams -- that is pinned_transport's concern.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import socket
from collections.abc import Callable

from crawlutron.models.host_policy import allow_private_targets
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.runtime import process_state
from crawlutron.safety.bounded_ttl import BoundedTTLCache
from crawlutron.safety.validation import is_public_ip_value

__all__ = [
    "DNS_WAIT_SECONDS",
    "DNS_WORKERS",
    "ResolverBudgetExceeded",
    "bounded_addrinfo",
    "bounded_thread_resolve",
    "check_ssrf_protection",
    "check_ssrf_protection_stable",
    "dns_in_flight_resolves",
    "dns_wait_for",
    "is_private_or_loopback_ip",
    "resolve_and_pin_host",
]


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
    # The CONCURRENT future is what stays undone until the worker thread
    # returns; the asyncio wrapper the caller awaits is cancelled by the wait
    # timeout, so registering it would re-open the blind window this closes.
    concurrent_future = _DNS_EXECUTOR.submit(work)
    _register_dns_future(concurrent_future)
    future = asyncio.wrap_future(concurrent_future)
    try:
        return await asyncio.wait_for(future, timeout=wait_for)
    except TimeoutError:
        concurrent_future.cancel()
        raise ResolverBudgetExceeded(f"DNS resolution exceeded the {wait_for:.1f}s budget") from None


# Per-host DNS cache; the TTL is short so rebinds stay bounded, with IP
# re-validation on every fresh resolution. One locked store: crawlutron.safety.bounded_ttl.
_DNS_TTL = 60.0
_DNS_CACHE_MAX = 4096
_DNS_CACHE: BoundedTTLCache[str, list[str]] = BoundedTTLCache(max_entries=_DNS_CACHE_MAX, ttl=_DNS_TTL)

# A RUNNING getaddrinfo cannot be cancelled, so a hostile/slow resolver can
# occupy every worker; queued resolves then each wait the full budget -- the
# request path hostage to a queue (#C10). Saturation law: when every worker is
# inside a live resolve, a new resolve refuses FAST instead of queueing. Both
# DNS entry points register here (sync concurrent + async asyncio-wrapped,
# pass 5); the registry is event-loop-side only, so it needs no lock.
_DnsFuture = concurrent.futures.Future | asyncio.Future
_dns_futures: set[_DnsFuture] = set()

# Dedicated thread pool for blocking DNS: concurrent fetches never stall the
# event loop or contend with the default executor used for extraction.
DNS_WORKERS = 32
_DNS_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
    max_workers=DNS_WORKERS, thread_name_prefix="dns-resolve"
)


def _refuse_if_saturated() -> None:
    """The saturation law, ONE copy (#C10): all workers inside live resolves =>
    a new resolve refuses FAST instead of queueing behind uncancellable threads."""
    if len(_dns_futures) >= DNS_WORKERS:
        raise ResolverBudgetExceeded(
            f"DNS resolver saturated ({DNS_WORKERS} workers busy); refusing to queue"
        )


def _register_dns_future(future: _DnsFuture) -> None:
    """Account a resolve until its RESOLVER THREAD completes, not until the
    caller's wait times out (wave-3 N1): a getaddrinfo still running inside a
    worker is exactly the saturation the chokepoint must keep counting. Both
    future kinds support add_done_callback, so one registration covers both paths."""
    _dns_futures.add(future)
    future.add_done_callback(_dns_futures.discard)


def bounded_addrinfo(hostname: str) -> list[str]:
    """One getaddrinfo on the shared DNS executor, bounded by the DNS posture: a
    resolve that outlasts the budget raises the classifiable ResolverBudgetExceeded
    instead of waiting unbounded on the caller's thread. Saturated => refuse fast (#C10)."""
    wait_for = dns_wait_for(None)
    _refuse_if_saturated()
    future = _DNS_EXECUTOR.submit(socket.getaddrinfo, hostname, None)
    _register_dns_future(future)
    try:
        done, _ = concurrent.futures.wait({future}, timeout=wait_for)
    except BaseException:
        # Symmetry with the callback: a synchronous failure (KeyboardInterrupt,
        # event teardown) must not leave a stale future holding a registry slot.
        _dns_futures.discard(future)
        raise
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


def _reset() -> None:
    """Drop every memoized DNS resolution: the cache is process-wide with a 60s
    TTL, so a test that resolves once is served the same answer all session."""
    _DNS_CACHE.clear()
    for future in list(_dns_futures):
        future.cancel()
    _dns_futures.clear()


def _has() -> bool:
    """Whether any hostname's resolution is memoized right now."""
    return bool(_DNS_CACHE) or bool(_dns_futures)


process_state.register(__name__, _reset, _has)


def dns_in_flight_resolves() -> int:
    """Public door over the saturation accounting: live resolver workers now.

    Tests drive saturation through the real resolve door (a parked getaddrinfo,
    see tests._transport_doubles.ParkedResolver); this door only observes.
    """
    # The callback may discard from the completing thread mid-iteration, so
    # the accounting iterates over a snapshot.
    return sum(1 for f in tuple(_dns_futures) if not f.done())


# The DNS wait posture: the ceiling on the resolver wait, derived from
# the request's budget by ``dns_wait_for`` so a tight request tightens it.
DNS_WAIT_SECONDS = 5.0


def dns_wait_for(timeout_seconds: float | None) -> float:
    """The resolver wait for a request with ``timeout_seconds`` of budget:
    the request's own budget when it is the tighter one, else the DNS posture."""
    if timeout_seconds is None:
        return DNS_WAIT_SECONDS
    return min(max(0.1, float(timeout_seconds)), DNS_WAIT_SECONDS)
