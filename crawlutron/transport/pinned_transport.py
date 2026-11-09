"""Owns request pinning, redirect targeting, and stream composition.

Carries the pinned primitives over an admitted URL (fresh pin, plain-HTTP/HTTPS
connect composition, redirect-chain rules); never retries, extracts, or
constructs. SSRF validation and DNS resolution live in ``pinned_dns``.
"""

from __future__ import annotations

import typing
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import urlparse

import httpx

from crawlutron.models.json_value import JsonValue
from crawlutron.models.normalized_url import NormalizedUrl
from crawlutron.safety.header_values import reject_injectable_header_values
from crawlutron.transport import pinned_dns as _dns
from crawlutron.transport.cookie_jar import COOKIE_NAMESPACE, global_cookie_jar


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
    return PinnedRequest(connect_url=connect_url, headers=out_headers, url=url)


def apinned_stream(
    client: httpx.AsyncClient,
    chain: RedirectChain,
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
        extensions=_sni_extensions(request),
    )


def _sni_extensions(request: PinnedRequest) -> dict[str, str] | None:
    """The SNI extension mapping for a pinned request: the logical hostname.
    ``None`` when the hop has nothing to pin (plain HTTP or the local-dev posture)."""
    return {"sni_hostname": sni} if (sni := request.tls_hostname) else None


@dataclass(frozen=True, slots=True)
class PinnedBody:
    """The request body a pinned call sends: exactly one arm is set.

    One record instead of three parallel optional parameters, so no call site
    mirrors a helper's argument list to send a body.
    """

    content: bytes | None = None
    json_value: dict[str, JsonValue] | None = None
    form: dict[str, str] | None = None

    def __post_init__(self) -> None:
        if sum(arm is not None for arm in (self.content, self.json_value, self.form)) > 1:
            raise ValueError("PinnedBody carries at most one body arm")


def open_pinned_request(
    client: httpx.AsyncClient,
    method: str,
    request: PinnedRequest,
    *,
    timeout: httpx.Timeout | None = None,
    headers: dict[str, str] | None = None,
    body: PinnedBody | None = None,
) -> typing.AsyncContextManager[httpx.Response]:
    """Open one SSRF-pinned httpx stream by method: the connect URL is the
    validated IP and TLS SNI stays on the logical hostname. The CALLER owns the
    response drain -- a body read inside the context -- so each site keeps its
    own cap posture (webhook warns-and-continues, search refuses)."""
    return client.stream(
        method,
        request.connect_url,
        headers=headers if headers is not None else request.headers,
        timeout=timeout,
        content=body.content if body else None,
        json=body.json_value if body else None,
        data=body.form if body else None,
        extensions=_sni_extensions(request),
    )


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


class RedirectChain:
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
        return await _dns.bounded_thread_resolve(
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
) -> AsyncIterator[tuple[RedirectChain, PinnedRequest]]:
    """Yield each hop of the manual redirect walk, already SSRF-checked and re-pinned.

    The body receives ``(chain, pinned_request)``, sends, then leaves;
    ``continue`` after ``chain.advance(response)`` asks for the next hop.
    """
    # timeout_seconds is the request budget; it bounds the resolver wait.
    # The shared wire edge: a value that can start another header is refused here.
    reject_injectable_header_values(headers)
    chain = RedirectChain(url, headers, pin=pin or build_pinned_request, timeout_seconds=timeout_seconds)
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
