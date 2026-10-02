from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.addresses import AddressClass, classify
from app.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason
from app.models.normalized_url import NormalizedUrl

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
    reason = host_policy_reason(host, policy=policy) or host_egress_reason(host)
    if reason:
        raise ValueError(reason)

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


def is_public_ip_value(value: str) -> bool:
    """Return whether a host string is safe for public egress.

    Unlike ``app.models.host_policy._host_is_public`` this is asked of a
    *resolved* address, so a string that is not an address at all is not public
    here.
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
    "filter_js_endpoints",
    "is_public_ip_value",
    "normalize_public_http_url",
    "public_url_or_none",
    "safe_public_url_or_none",
]
