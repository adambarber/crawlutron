"""Centralized header control: one owner decides who may name a header in
``policy.headers`` (``refusal_reason``) and whether a header survives an origin
boundary (``survives_cross_origin``).
"""

from __future__ import annotations

from crawlutron.vocabulary.identity import DEFAULT_USER_AGENT

#: Header classes a caller may not nominate in ``policy.headers``; each class
#: carries the refusal reason its members share.
_HEADER_CLASSES: tuple[tuple[frozenset[str], str], ...] = (
    (
        frozenset({"if-none-match", "if-modified-since"}),
        "conditional headers belong to the validators map",
    ),
    (
        frozenset({"cookie"}),
        "the cookie jar owns Cookie, and request_profile.cookies is its sanctioned "
        "second door; a raw header bypasses both",
    ),
    (
        frozenset({"host"}),
        "Host is owned by the pinned-request chokepoint and is never forwarded verbatim",
    ),
    (
        frozenset({"proxy-authorization"}),
        "proxy credentials are not a crawl input",
    ),
    (
        frozenset({"authorization"}),
        "the origin-scoped credential door is policy.credentials; a raw header "
        "bypasses its origin scoping and redirect re-scoping",
    ),
    (
        frozenset({"x-forwarded-for", "x-forwarded-host", "x-real-ip"}),
        "origin-side identity headers must not be caller-controlled",
    ),
    (
        frozenset({"content-length", "transfer-encoding"}),
        "body framing belongs to the HTTP client, not to a crawl input",
    ),
    (
        frozenset({"connection"}),
        "hop-by-hop header; the connection is not the caller's to describe",
    ),
    (
        frozenset({"keep-alive"}),
        "hop-by-hop header; connection persistence is the client's decision",
    ),
    (
        frozenset({"te"}),
        "hop-by-hop header; transfer coding is negotiated per connection",
    ),
    (
        frozenset({"trailer"}),
        "hop-by-hop header; trailers are framed by the client, not declared here",
    ),
    (
        frozenset({"upgrade"}),
        "hop-by-hop header; protocol upgrade is not a crawl input",
    ),
    (
        frozenset({"proxy-connection", "expect", "proxy-authenticate"}),
        "hop-by-hop header; the connection is not the caller's to describe",
    ),
    (
        frozenset({"via", "forwarded"}),
        "forwarding metadata; the hop's path is not the caller's to describe",
    ),
)

#: Service-owned identity headers that survive an origin boundary; closed by
#: construction so the assembler and the redirect scrub cannot disagree.
SERVICE_OWNED_IDENTITY_HEADERS: frozenset[str] = frozenset(
    {
        "user-agent",
        "accept",
        "accept-language",
        "accept-encoding",
        "sec-fetch-dest",
        "sec-fetch-mode",
        "sec-fetch-site",
    }
)


def refusal_reason(name: str) -> str | None:
    """Why a caller may not name ``name`` in ``policy.headers``, or None.

    Class-derived: membership in ``_HEADER_CLASSES`` decides; the ``x-forwarded-*``
    prefix catches every forwarding name the classes do not name individually.
    """
    lowered = name.strip().lower()
    for nameset, reason in _HEADER_CLASSES:
        if lowered in nameset:
            return reason
    if lowered.startswith("x-forwarded-"):
        return "forwarding metadata; the hop's path is not the caller's to describe"
    return None


def service_identity_headers() -> dict[str, str]:
    """The service-owned identity headers a request presents, name to default
    value. Built from ``SERVICE_OWNED_IDENTITY_HEADERS``; dynamic signals
    (user-agent rotation, the jar cookie) are overlaid by the assembler.
    """
    return {
        "User-Agent": DEFAULT_USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
    }


def survives_cross_origin(name: str) -> bool:
    """Whether ``name`` survives a cross-origin hop.

    Only service-owned identity headers survive; a caller-supplied name such as
    ``X-Api-Key`` is dropped. Closed by construction.
    """
    return name.strip().lower() in SERVICE_OWNED_IDENTITY_HEADERS
