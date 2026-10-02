from __future__ import annotations

import logging
import os
from functools import lru_cache
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.addresses import AddressClass, classify
from app.env import parse_bool_env
from app.models.host_policy import HostPolicy, host_policy_reason
from app.models.normalized_url import NormalizedUrl

PRIVATE_HOSTS = {"localhost", "host.docker.internal"}

# Attribution/tracking query parameters that carry no content-identity
# information. Dropping them lets URLs that differ only by UTM/tracking tags
# canonicalize to the same key, preventing duplicate fetches of the same page
# Bare ``ref`` is intentionally excluded: it is ambiguous and frequently
# a legitimate content parameter, so stripping it could collapse distinct pages.
_TRACKING_PARAM_EXACT = frozenset(
    {
        "gclid",
        "gclsrc",
        "fbclid",
        "gbraid",
        "wbraid",
        "mc_cid",
        "mc_eid",
        "mkt_tok",
        "igshid",
        "_hsenc",
        "_hsmi",
        "hsctat",
    }
)


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k.startswith("utm_") or k in _TRACKING_PARAM_EXACT


def normalize_public_http_url(url: str, *, policy: HostPolicy) -> NormalizedUrl:
    """Canonicalize a public HTTP(S) URL into a single normalized form.

    This is the single source of truth for what a normalized URL is across the
    service: it is used for the crawl target URL and for every discovered link.
    Canonicalization lowercases the scheme and host, drops default ports
    (``:80`` for http, ``:443`` for https), removes the fragment, strips a
    trailing slash from the path (so ``https://example.gov/`` and
    ``https://example.gov`` are identical), and re-encodes the query string.
    """
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")

    host = parsed.hostname.lower().rstrip(".")
    reason = host_policy_reason(host, policy=policy)
    if reason:
        raise ValueError(reason)
    # Local dev/test escape hatch: the crawler is a localhost dev service and
    # tests run a real in-process server on loopback. Allow opting out of the
    # private-network guard via env var rather than relaxing it by default.
    # Inverted-by-default: any value not explicitly recognized as truthy keeps
    # the SSRF guard ON, using the shared ``parse_bool_value`` policy so every
    # path agrees on what counts as "on". Both layers below are memoized
    # because this function runs per anchor: the env decision
    # is keyed on its raw value (so any change is visible on the very next
    # call) and the host verdict is a pure function of the canonical host.
    if not allow_private_targets() and not _host_is_public(host):
        raise ValueError("private_network_target")

    default_port = 80 if parsed.scheme == "http" else 443
    port = parsed.port
    if port == default_port:
        port = None
    netloc = host if port is None else f"{host}:{port}"
    path = _strip_trailing_colon(parsed.path or "").rstrip("/")
    query = _strip_trailing_colon(parsed.query)
    if query:
        # Drop known tracking/attribution parameters so URLs that differ only
        # by UTM/tracking tags canonicalize to the same key and are not fetched
        # as duplicate pages. Remaining params are sorted so URLs differing
        # only in parameter order also canonicalize identically.
        pairs = [(k, v) for (k, v) in parse_qsl(query, keep_blank_values=True) if not _is_tracking_param(k)]
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    # The construction point of the type: everything upstream is raw
    # input; everything downstream that names this shape is typed.
    return NormalizedUrl(urlunparse((parsed.scheme, netloc, path, parsed.params, query, "")))


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


# (raw env value, parsed decision) for CRAWLUTRON_ALLOW_PRIVATE_TARGETS.
# Value-keyed, not time-keyed: an env change (including a test monkeypatch)
# takes effect on the very next call; an unchanged value skips the parse.
_PRIVATE_TARGET_DECISION: tuple[str | None, bool] | None = None


def allow_private_targets() -> bool:
    """Memoized read of the private-target escape hatch (inverted default).

    The raw env value is the cache key, so unrecognized values keep resolving
    to ``False`` (guard ON) through the same ``parse_bool_env`` chokepoint and
    every distinct value change is honored immediately.
    """
    global _PRIVATE_TARGET_DECISION
    raw = os.environ.get("CRAWLUTRON_ALLOW_PRIVATE_TARGETS")
    cached = _PRIVATE_TARGET_DECISION
    if cached is not None and cached[0] == raw:
        return cached[1]
    decision = parse_bool_env("CRAWLUTRON_ALLOW_PRIVATE_TARGETS", default=False)
    _PRIVATE_TARGET_DECISION = (raw, decision)
    if decision:
        # Warned at the decision, not at each call site: this runs on the
        # per-fetch SSRF path, so a warning here would be a log flood. The memo
        # makes it once per distinct env value, so a deployment that sets the
        # variable logs exactly once and a test that re-sets it still sees it.
        logging.getLogger(__name__).warning(
            "SSRF private-target guard DISABLED by CRAWLUTRON_ALLOW_PRIVATE_TARGETS=%r: "
            "caller-supplied URLs may now reach private, loopback, and link-local "
            "addresses, including cloud metadata endpoints. Unset it, or set it to 0, "
            "outside local development.",
            raw,
            extra={"env_name": "CRAWLUTRON_ALLOW_PRIVATE_TARGETS", "env_value": raw},
        )
    return decision


def _reset_guard_memo() -> None:
    """Drop the memoized env decision (tests that count parse calls only).

    Host verdicts are a pure function of the host and never go stale, so the
    verdict memo deliberately is not touched here.
    """
    global _PRIVATE_TARGET_DECISION
    _PRIVATE_TARGET_DECISION = None


@lru_cache(maxsize=8192)
def _host_is_public(host: str) -> bool:
    """SSRF verdict for one canonical host (bounded memo, pure function).

    Deterministic for a given host and independent of the env hatch (only
    consulted while the guard is ON), so cache entries never go stale. The
    bound keeps attacker-influenced host floods from growing memory.
    """
    if host in PRIVATE_HOSTS:
        return False
    # A host that is not an address at all is a name, and what it resolves to
    # is judged separately against ``is_public_ip_value`` once the resolver
    # has answered. Only a literal that lands in a rejected range is refused.
    address_class = classify(host)
    return address_class in (AddressClass.NOT_AN_ADDRESS, AddressClass.PUBLIC)


def is_public_ip_value(value: str) -> bool:
    """Return whether a host string is safe for public egress.

    Unlike ``_host_is_public`` this is asked of a *resolved* address, so a
    string that is not an address at all is not public here.
    """
    return classify(value) is AddressClass.PUBLIC


def public_url_or_none(
    raw: str | None,
    *,
    resolve_against: str | None = None,
    policy: HostPolicy,
) -> str | None:
    """Egress filter: resolve relatives FIRST, then strictly accept http/https.

    Attacker-influenced URLs leave the service through several doors
    (js_endpoints, next_page_url, sitemap locs, discovered links), each
    filtered slightly differently. The recurring inconsistency: a bare
    ``urlparse(url).scheme`` check treats protocol-relative (``//evil.com/x``)
    and relative (``/api``) URLs as scheme ``""`` -- one site allowed ``""``
    (emitting unresolvable URLs), another dropped it (killing relative
    pagination). Resolving against the source URL first makes both cases
    absolute, after which only http/https survive; ``javascript:``/``data:``/
    ``ftp:`` are always None, as is anything unresolvable.
    """
    if not raw or not raw.strip():
        return None
    candidate = raw.strip()
    if resolve_against:
        candidate = urljoin(resolve_against, candidate)
    normalized = safe_public_url_or_none(candidate, policy=policy)
    return str(normalized) if normalized is not None else None


def filter_js_endpoints(
    endpoints: list[dict[str, str]],
    *,
    resolve_against: str | None = None,
    policy: HostPolicy,
) -> list[dict[str, str]]:
    """Egress filter for the JS-endpoint discovery door.

    ``extract_js_endpoints`` scrapes URLs out of attacker-controlled HTML/JS
    and hands back ``{"url": ...}`` dicts. Two builders (HTTP and browser)
    each filtered that list with the same inline comprehension -- the shape
    that drifts the moment a third consumer forgets the copy. This is the
    single filter for the door: an endpoint survives only if its URL passes
    :func:`public_url_or_none` (resolve relatives first, then accept only
    http(s)), and the surviving endpoint carries the filtered URL so callers
    never see the raw scraped string.
    """
    return [
        {**endpoint, "url": clean}
        for endpoint in endpoints
        if (
            clean := public_url_or_none(
                str(endpoint.get("url", "")),
                resolve_against=resolve_against,
                policy=policy,
            )
        )
        is not None
    ]


def safe_public_url_or_none(raw: str | None, *, policy: HostPolicy) -> NormalizedUrl | None:
    """The single normalize-or-None chokepoint for URL egress doors.

    ``normalize_public_http_url`` raises ``ValueError`` on anything it rejects
    (private networks under SSRF policy, non-http schemes, un-normalizable
    input). Callers on egress doors -- search results, map links, batch dedupe,
    sitemap seeds -- all want the same thing: the canonical public URL, or
    ``None`` to drop the entry. Each one hand-rolled the same
    ``try/except ValueError: return None``, which is how a new door ships
    without the SSRF policy: the wrapper is easy to forget and easy to get
    subtly wrong, and nothing tests the absence.

    ``resolve_against`` is intentionally absent: callers that must resolve a
    relative URL first do so via :func:`public_url_or_none`, then pass the
    result here. Combining the two would make this function decide between
    two egress contracts, and the policy must apply identically at every door.
    """
    try:
        if raw is None:
            return None
        return normalize_public_http_url(raw, policy=policy)
    except ValueError:
        return None


__all__ = [
    "PRIVATE_HOSTS",
    "allow_private_targets",
    "filter_js_endpoints",
    "is_public_ip_value",
    "normalize_public_http_url",
    "public_url_or_none",
    "safe_public_url_or_none",
]
