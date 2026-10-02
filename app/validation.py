from __future__ import annotations

from urllib.parse import urljoin

from app.addresses import AddressClass, classify
from app.models.host_policy import HostPolicy
from app.models.normalized_url import NormalizedUrl, normalize_public_http_url


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
    "public_url_or_none",
    "safe_public_url_or_none",
]
