"""The vocabulary a crawl result is written in: ``Classification``,
``CrawlError`` (a union that includes it), and ``CrawlStrategyName``; the runtime
set and mapping below are DERIVED from these aliases, one declaration read three ways.
"""

from __future__ import annotations

import types
from typing import Any, Literal, Union, get_args, get_origin

# ``X | Y`` and ``Union[X, Y]`` are the same type with two spellings and two
# origins; a taxonomy alias written either way has to read the same here.
_UNION_ORIGINS = (Union, types.UnionType)

Classification = Literal[
    "rendered",
    "captcha_detected",
    "bot_challenge_detected",
    "access_denied",
    "login_required",
    "paywall_detected",
    "rate_limited",
    "cookie_consent_required",
    "javascript_required",
    "unsupported_browser",
    "geolocation_blocked",
    "waf_challenge_detected",
    "not_found",
    "site_error",
    "gone",
    "binary_no_text",
    "robots_disallowed",
    "navigation_timeout",
    "browser_crash",
    "network_error",
    "connection_error",
    "http_status_error",
    # The per-URL budget overran: the host answered but the chain exceeded
    # total_url_budget_seconds. A member so it survives the strict enum.
    "url_budget_exceeded",
    # Fetch-failure codes the HTTP strategy's error classifier emits. Members
    # so they survive the strict enum field at response shaping.
    "payload_too_large",
    "too_many_redirects",
    "http_error",
    "value_error",
    "tls_error",
    "dns_error",
    "invalid_url",
    "unsupported_scheme",
    "private_network_target",
    "unknown_error",
    "not_modified",
    # A browser renderer slot was unavailable within the acquire bound, so the
    # URL was not attempted; the frontier re-queues it (at-least-once).
    "browser_pool_exhausted",
    # A domain's circuit breaker was open: a scheduler capacity decision, not
    # a page outcome -- a non-member code would be downgraded, hiding it.
    "circuit_breaker_open",
]

# Pipeline conditions with no page-classification analogue (capacity, config):
# they are not page outcomes, so forcing them in would distort the taxonomy.
PIPELINE_ONLY_CRAWL_ERRORS = Literal[
    # Deliberately NOT spelled like its robots_disallowed Classification
    # member: this is the stable ERROR code, asserted disjoint from the set.
    "robots_txt_disallowed",
    "browser_worker_unavailable",
    "unsupported_strategy",
    "retries_exhausted",
]

#: The stable failure code beside ``classification``: a declared union of the
#: two groups (hand-maintained, since mypy cannot compute ``*get_args()``).
CrawlError = Classification | PIPELINE_ONLY_CRAWL_ERRORS

CrawlStrategyName = Literal[
    "HTTP_DECLARED_CRAWLER",
    "HTTP_CFFI_IMPERSONATE",
    "BROWSER_HEADLESS",
    "BROWSER_HEADED_WARMED",
]


def literal_members(alias: Any) -> frozenset[str]:
    """The string members of a declared type, total by origin: a ``Literal``
    yields its args, a union the union of what it holds, and anything else
    nothing -- the clause that makes a field sweep safe to point it at.
    """
    origin = get_origin(alias)
    if origin is Literal:
        return frozenset(arg for arg in get_args(alias) if isinstance(arg, str))
    if origin in _UNION_ORIGINS:
        members: set[str] = set()
        for arg in get_args(alias):
            members |= literal_members(arg)
        return frozenset(members)
    return frozenset()


_CLASSIFICATION_TABLE: dict[str, Classification] = {name: name for name in get_args(Classification)}


def as_classification(value: str, *, default: Classification = "unknown_error") -> Classification:
    """Return ``value`` as a :data:`Classification`, or ``default`` if it is not
    one -- so an untriaged code from a string boundary degrades instead of
    widening the type. The table is built from the alias's own Literal members.
    """
    return _CLASSIFICATION_TABLE.get(value, default)
