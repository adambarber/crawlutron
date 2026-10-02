"""The canonical form of a URL that has crossed admission.

Constructing a ``NormalizedUrl`` IS the admission decision: the constructor
validates the scheme and the host against the policy and the egress guard,
canonicalizes the string, and derives the host and the registrable site. There is
no other way to build one, so every position that admits a URL for queueing can
say ``NormalizedUrl`` in its own signature and mean it.

The three answers a URL carries -- its canonical string, its host, its site --
are read off the value rather than recomputed at each use. The string itself
stays available as ``raw`` for the wire, cache keys and spill JSON, which is the
one place a URL becomes text again.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from app.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason
from app.models.registrable_domain import registrable_domain

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


def _canonical_parts(url: str, *, policy: HostPolicy) -> tuple[str, str, str]:
    """The canonical string, its host and its site, or raise with the refusal reason."""
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
    canonical = urlunparse((parsed.scheme, netloc, path, parsed.params, query, ""))
    return canonical, host, registrable_domain(canonical)


class NormalizedUrl:
    """An admitted URL: canonical, and carrying its host and its site.

    Immutable and compared by canonical string, so it can key a map or a cache
    the way the bare string used to. ``raw`` is the one explicit place a URL
    turns back into text.
    """

    __slots__ = ("_host", "_raw", "_site")

    def __init__(self, raw: str, *, policy: HostPolicy) -> None:
        self._raw, self._host, self._site = _canonical_parts(raw, policy=policy)

    @property
    def raw(self) -> str:
        """The canonical string: the wire form, the cache key, the spill field."""
        return self._raw

    @property
    def host(self) -> str:
        """The canonical host, which is what the scheduler's per-host gates key on."""
        return self._host

    @property
    def site(self) -> str:
        """The registrable domain, which is what the scheduler's per-site gates key on."""
        return self._site

    def __str__(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"NormalizedUrl({self._raw!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NormalizedUrl) and other._raw == self._raw

    def __hash__(self) -> int:
        return hash(self._raw)


def normalize_public_http_url(url: str, *, policy: HostPolicy) -> NormalizedUrl:
    """Canonicalize a public HTTP(S) URL into a single normalized form.

    Thin alias for the constructor, kept only while callers migrate.
    """
    return NormalizedUrl(url, policy=policy)


__all__ = ["NormalizedUrl", "normalize_public_http_url"]
