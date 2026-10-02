"""The canonical form of a URL that has crossed the normalizer.

A ``NewType`` over ``str``: erased at runtime -- spill JSON, cache keys, and the
wire keep plain strings -- but mypy rejects putting a raw request/sitemap/crawl
string into a normalized-URL position unless it has crossed
``normalize_public_http_url``. That is the whole point of the nominal wrapper: it
is the one place the tree can say "this string has been admitted", and every
position that admits a URL for queueing can say so in its own signature.

The normalizer lives here rather than in ``app/validation.py`` because a URL's
identity -- which host it is, which site it belongs to, what its canonical
string is -- is one fact with three answers, and it belongs beside the type that
carries it.
"""

from __future__ import annotations

from typing import NewType
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason

NormalizedUrl = NewType("NormalizedUrl", str)

#: Attribution/tracking query parameters that carry no content-identity
#: information. Dropping them lets URLs that differ only by UTM/tracking tags
#: canonicalize to the same key, preventing duplicate fetches of the same page.
#: Bare ``ref`` is intentionally excluded: it is ambiguous and frequently a
#: legitimate content parameter, so stripping it could collapse distinct pages.
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


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


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


__all__ = ["NormalizedUrl", "normalize_public_http_url"]
