"""The vocabulary a crawl result is written in.

One concept, four declarations: ``Classification`` says how a page turned out,
``PIPELINE_ONLY_CRAWL_ERRORS`` says which failures are pipeline capacity or
config decisions rather than page outcomes, ``CrawlError`` is the stable code
carried beside the classification, and ``CrawlStrategyName`` names the strategies
that can produce a result. They live together because ``CrawlError`` is *defined*
as a union of the first two: separate them and the union's inputs are files the
reader has to open before the line makes sense.

The runtime set and the mapping below are DERIVED from the aliases rather than
restated, which is the whole point of the module. ``mypy`` cannot evaluate
``*get_args()`` inside ``Literal[...]``, so ``CrawlError`` used to spell every
``Classification`` member out a second time and a test held the copy in step --
a hand-kept mirror of the alias above it, re-derived every time a code was
added. Here the static type, the runtime member set, and the taxonomy are one
declaration read three ways.
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
    # The per-URL budget overran: the host answered but the whole chain
    # exceeded total_url_budget_seconds. A CrawlClassification member (see
    # app/vocabulary/taxonomy.py) so it survives the strict enum at response shaping.
    "url_budget_exceeded",
    # Fetch-failure codes the HTTP strategy's error classifier emits; they are
    # CrawlClassification members (see app.models.taxonomy) so they survive the
    # strict enum field at response shaping.
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
    # URL was not attempted; the frontier re-queues it (at-least-once). A
    # CrawlClassification member so it survives the strict enum at response
    # shaping.
    "browser_pool_exhausted",
    # A domain's circuit breaker was open, so the URL was not attempted: a
    # scheduler capacity decision, not a page outcome. A CrawlClassification
    # member for the same reason -- a non-member code would reach the strict
    # enum and be downgraded to site_error, hiding the skip reason.
    "circuit_breaker_open",
]

# Pipeline conditions with no page-classification analogue: a capacity decision
# and a config one. They are not page outcomes, so forcing them into
# Classification would distort that taxonomy.
PIPELINE_ONLY_CRAWL_ERRORS = Literal[
    # Deliberately not spelled like its ``robots_disallowed`` Classification
    # member: this is the stable ERROR code, which may differ from the
    # classification of the same event, and the taxonomy invariant asserts the
    # two sets stay disjoint. ``request_pipeline._robots_disallowed`` sets the
    # pair (``classification="robots_disallowed"``,
    # ``error="robots_txt_disallowed"``); the two strings are not interchangeable.
    "robots_txt_disallowed",
    "browser_worker_unavailable",
    "unsupported_strategy",
    "retries_exhausted",
]

# The stable machine-readable failure code carried alongside ``classification``.
# Documented at app/api/render.py:202 as a stable code callers can key on, so it
# is a declared taxonomy rather than a free-form str: a code that is not listed
# here cannot reach a result, and the membership sweep enforces it for free.
# - page-outcome codes that are also Classification members: the builders route
#   ``error=classification`` for these (result_helpers), so the two fields agree
#   and the code is a stable key for callers.
# - pipeline conditions with no page-classification analogue
#   (``browser_worker_unavailable`` is a capacity decision, ``unsupported_strategy``
#   a config one): they are not page outcomes and forcing them into Classification
#   would distort that taxonomy.
#
# It is the UNION of the two, not a restatement of the first, and the union is
# what makes it evaluable: mypy cannot compute `*get_args()` inside `Literal[...]`,
# so a restatement had to be kept in step by a guard instead of derived.
CrawlError = Classification | PIPELINE_ONLY_CRAWL_ERRORS

CrawlStrategyName = Literal[
    "HTTP_DECLARED_CRAWLER",
    "HTTP_CFFI_IMPERSONATE",
    "BROWSER_HEADLESS",
    "BROWSER_HEADED_WARMED",
]


def literal_members(alias: Any) -> frozenset[str]:
    """The string members of a declared type, whatever shape it composes in.

    The one place a declared type crosses into a runtime membership check, and
    the reason it is total rather than a ``get_args`` call: ``get_args`` answers
    different questions for different annotations. For a bare ``Literal`` it
    returns the members; for a union it returns the union's arguments
    UNflattened, so the members arrive as nested alias objects; and for
    ``dict[str, str]`` it returns ``(str, str)``, which a naive recursive walk
    reads as two members named "str".

    So the origin decides. A ``Literal`` contributes its string args, a union
    contributes the union of what it holds (which is what lets ``CrawlError``
    above be *defined* as a union of the two taxonomies instead of restating one
    of them), and anything else is not a taxonomy and contributes nothing. That
    last clause is the load-bearing one: it is what makes this safe to point at
    an arbitrary field annotation, which is what a field sweep does.
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
    """Return ``value`` as a:data:`Classification`, or ``default`` if it is not one.

    Callers that pull a code off a string boundary (an exception message, a
    cached dict value) route through here rather than asserting, so an
    untriaged code degrades to the documented default instead of widening the
    classification type at every consumer. The table is built from the alias's
    own members so the mapping and the enum cannot drift; its values are the
    Literal members, which is what makes the return provably narrow.
    """
    return _CLASSIFICATION_TABLE.get(value, default)
