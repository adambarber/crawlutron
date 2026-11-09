"""The canonical form of a URL that has crossed admission: constructing one IS
admission -- scheme/host validated against policy and the egress guard, with no
other way to build it. Host and site ride the value; ``raw`` is the text form.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from crawlutron.models.host_canonical import idna_host
from crawlutron.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason
from crawlutron.models.registrable_domain import registrable_domain
from crawlutron.runtime.resource_lifecycle import record_mapped_value_error

#: Tracking/ad/social-feed infrastructure: recorded as links, never enqueued; apex
#: or any subdomain. Dotted-suffix tuple so ``endswith`` is one C-level call.
ANALYTICS_HOST_SUFFIXES = frozenset(
    {
        "google-analytics.com",
        "googletagmanager.com",
        "doubleclick.net",
        "googlesyndication.com",
        "googletagservices.com",
        "adservice.google.com",
        "facebook.net",
        "connect.facebook.net",
        "analytics.twitter.com",
        "x.com",
        "pinterest.com",
        "hotjar.com",
        "scorecardresearch.com",
        "mixpanel.com",
        "amplitude.com",
        "segment.io",
        "segment.com",
        "criteo.com",
        "pubmatic.com",
        "rubiconproject.com",
        "adroll.com",
    }
)
_ANALYTICS_DOTTED = tuple("." + s for s in ANALYTICS_HOST_SUFFIXES)

#: Non-HTML resource extensions: no further HTML links; discovery records them.
DOCUMENT_URL_EXTENSIONS = frozenset(
    {".pdf", ".csv", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".rtf"}
)
_DOCUMENT_URL_EXTENSIONS = tuple(sorted(DOCUMENT_URL_EXTENSIONS))

#: Tracking/attribution parameters that carry no content identity; dropping them
#: canonicalizes UTM variants to one key. Bare ``ref`` stays: ambiguous, often content.
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


#: The refusal-shaped literals ``_canonical_parts`` legitimately raises: a
#: ValueError carrying one of these IS an admission refusal; anything else
#: exiting the constructor is foreign (mapped at the boundary, never leaked).
_REFUSAL_LITERALS = frozenset(
    {
        "unsupported_scheme",
        "invalid_url",
        "userinfo_refused",
        "ipvfuture_refused",
        "invalid_port",
        "invalid_idn_host",
        # Variable-held verdicts the policy/egress arms raise through the
        # constructor (``raise ValueError(reason)`` at _canonical_parts:101).
        "host_denied",
        "host_not_allowed",
        "private_network_target",
    }
)


def _bracketed_netloc(host: str, port: int | None) -> str:
    """Netloc with IPv6 literals re-bracketed; bare colons parse as host:port pairs."""
    host_part = f"[{host}]" if ":" in host else host
    return host_part if port is None else f"{host_part}:{port}"


def _canonical_parts(url: str, *, policy: HostPolicy) -> tuple[str, str, str, str, int | None, str, str]:
    """(canonical, scheme, host, site, port, path, query), or raise with the refusal reason."""
    try:
        parsed = urlparse(url)
    except ValueError as error:
        # Malformed brackets raise urllib's non-literal "Invalid IPv6 URL"
        # before any structural check runs; map it explicitly, never leak it.
        raise ValueError("invalid_url") from error
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")
    # Whitespace in the authority is not a host: urlparse drops control
    # characters and tolerates spaces, so the check rides the raw text.
    start = url.lower().find(f"{parsed.scheme}://")
    if start >= 0:
        rest = url[start + len(parsed.scheme) + 3 :]
        cuts = [i for i in (rest.find(c) for c in "/?#") if i >= 0]
        authority = rest[: min(cuts)] if cuts else rest
        if any(ch.isspace() for ch in authority):
            raise ValueError("invalid_url")
    if parsed.username or parsed.password:
        # Userinfo is not a crawlable identity: credentials belong in
        # policy.credentials (origin-scoped), never in the URL itself.
        raise ValueError("userinfo_refused")

    scheme = parsed.scheme
    host = idna_host(parsed.hostname)
    # A bracketed host that is not an IPv6 literal is IPvFuture (RFC 3986);
    # nothing here dials one, so refuse rather than de-bracket it into a hostname.
    if parsed.netloc.startswith("[") and ":" not in host:
        raise ValueError("ipvfuture_refused")
    reason = host_policy_reason(host, policy=policy) or host_egress_reason(host)
    if reason:
        raise ValueError(reason)

    default_port = 80 if scheme == "http" else 443
    try:
        port = parsed.port
    except ValueError as error:
        # urllib's message is attacker-influenced text; the wire carries the
        # classification, never str(error).
        raise ValueError("invalid_port") from error
    if port == default_port:
        port = None
    netloc = _bracketed_netloc(host, port)
    # Path and query are identity: a trailing colon or slash in them can name a
    # distinct resource, so only the authority segment is normalized here.
    path = parsed.path or ""
    query = parsed.query
    if query:
        # Drop known tracking parameters so UTM-variants canonicalize to one key;
        # the rest are sorted, so parameter order alone canonicalizes identically.
        pairs = [(k, v) for (k, v) in parse_qsl(query, keep_blank_values=True) if not _is_tracking_param(k)]
        pairs.sort()
        query = urlencode(pairs, doseq=True)
    canonical = urlunparse((scheme, netloc, path, parsed.params, query, ""))
    return canonical, scheme, host, registrable_domain(host), port, path, query


class NormalizedUrl:
    """An admitted URL: canonical, and carrying its host and its site.
    Immutable and compared by canonical string, so it keys maps and caches the way
    the bare string did; ``raw`` is the one explicit place it turns back into text.
    """

    __slots__ = ("_host", "_path", "_policy", "_port", "_query", "_raw", "_scheme", "_site")

    def __init__(self, raw: str, *, policy: HostPolicy) -> None:
        try:
            canonical, scheme, host, site, port, path, query = _canonical_parts(raw, policy=policy)
        except ValueError as error:
            # The constructor boundary: only a refusal-shaped literal leaves as
            # itself; a foreign ValueError (a shape the structural checks have
            # not been wrapped into a named refusal) is mapped EXPLICITLY to
            # "invalid_url" here, so urllib or a future refactor can never
            # leak its own message into the wire vocabulary. A non-ValueError
            # programming error still raises -- admission never swallows it.
            # A non-ValueError programming error still raises -- admission
            # never swallows it. The mapping itself is recorded (bounded,
            # structured fields only), so it is never a silent 422.
            reason = _refusal_literal(str(error))
            if reason != str(error):
                record_mapped_value_error(str(error))
            raise ValueError(reason) from error
        self._raw = canonical
        self._scheme = scheme
        self._host = host
        self._site = site
        self._port = port
        self._path = path
        self._query = query
        # The policy that admitted this value travels with it, so the object can
        # re-justify itself; per-job containers run one policy, so it is the right one.
        self._policy = policy

    @property
    def raw(self) -> str:
        """The canonical string: the wire form, the cache key, the spill field.
        Also the value's durable identity -- equality, hashing and persistence key
        on it, so two NormalizedUrls with the same canonical string are the same value.
        """
        return self._raw

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def origin(self) -> str:
        """``scheme://host[:port]`` -- the fetch origin for robots/sitemaps."""
        default_port = 80 if self._scheme == "http" else 443
        host_part = f"[{self._host}]" if ":" in self._host else self._host
        if self._port is None or self._port == default_port:
            return f"{self._scheme}://{host_part}"
        return f"{self._scheme}://{host_part}:{self._port}"

    @property
    def host(self) -> str:
        """The canonical host, which is what the scheduler's per-host gates key on."""
        return self._host

    @property
    def site(self) -> str:
        """The registrable domain, which is what the scheduler's per-site gates key on."""
        return self._site

    @property
    def tls_hostname(self) -> str | None:
        """The hostname TLS must attest on this origin: the canonical host on
        https, else None. An IP literal attests to itself -- no SNI, no alias."""
        if self._scheme != "https":
            return None
        try:
            ipaddress.ip_address(self._host)
        except ValueError:
            return self._host
        return None

    @property
    def is_analytics_host(self) -> bool:
        """True when this URL points at tracking/ad infrastructure (apex or any subdomain)."""
        return self._host in ANALYTICS_HOST_SUFFIXES or self._host.endswith(_ANALYTICS_DOTTED)

    @property
    def is_document_url(self) -> bool:
        """True when this URL's path names a known document resource (pdf, docx, ...)."""
        return self._path.lower().endswith(_DOCUMENT_URL_EXTENSIONS)

    def crosses_origin(self, current: NormalizedUrl) -> bool:
        """Whether a hop from ``current`` to self leaves the origin (the scheme/host/port
        law). Fails toward True: a kept credential header is unrecoverable, a stripped
        one only costs a re-login.
        """
        return self.origin != current.origin

    @property
    def port(self) -> int | None:
        return self._port

    @property
    def path(self) -> str:
        return self._path

    @property
    def query(self) -> str:
        return self._query

    @property
    def policy(self) -> HostPolicy:
        """The egress posture this value was admitted under."""
        return self._policy

    def resolve(self, reference: str | None) -> NormalizedUrl | None:
        """Join ``reference`` against this URL and admit the result.
        Every urljoin + egress-filter pair in the tree is this (resolve, then admit
        with the same policy); None when the reference is not a public page URL.
        """
        if not reference:
            return None
        try:
            return NormalizedUrl(urljoin(self._raw, reference), policy=self._policy)
        except ValueError:
            return None

    def __str__(self) -> str:
        return self._raw

    def __repr__(self) -> str:
        return f"NormalizedUrl({self._raw!r})"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NormalizedUrl) and other._raw == self._raw

    def __hash__(self) -> int:
        return hash(self._raw)

    @classmethod
    def __get_pydantic_core_schema__(cls, _source_type, handler):
        """Accept exactly one thing -- an already-admitted ``NormalizedUrl`` -- and render
        as the canonical string on the wire. A raw string has no policy to admit it,
        so refusing keeps admission at the constructor, the one place a policy exists.
        """
        from pydantic_core import core_schema

        def _validate(value: object) -> NormalizedUrl:
            if isinstance(value, cls):
                return value
            raise TypeError(f"{cls.__name__} is admitted by construction, not validated: pass the object")

        return core_schema.no_info_plain_validator_function(
            _validate,
            serialization=core_schema.plain_serializer_function_ser_schema(
                lambda url: url.raw, when_used="always"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(cls, core_schema, handler):
        """The wire form of an admitted URL is its canonical text.
        A plain-validator core schema carries no inferable JSON Schema, and the
        serializer is the truth to match; declaring it by hand keeps OpenAPI servable.
        """
        return {"type": "string"}


__all__ = ["NormalizedUrl", "admit_or_none", "admit_with_reason", "refusal_literals"]


def refusal_literals() -> frozenset[str]:
    """The refusal-shaped constructor vocabulary -- the boundary's public door."""
    return frozenset(_REFUSAL_LITERALS)


def _refusal_literal(reason: str) -> str:
    """Pass a refusal-shaped literal through; map a foreign one to ``invalid_url``
    explicitly (the constructor boundary's mapping, tested by the fuzz sweep)."""
    return reason if reason in _REFUSAL_LITERALS else "invalid_url"


def admit_or_none(candidate: str | NormalizedUrl | None, *, policy: HostPolicy) -> NormalizedUrl | None:
    """Admit ``candidate`` as a public page URL, or drop it.
    The constructor is the enforcement chokepoint; this is the one None-returning
    wrapper every egress door shares. An admitted value is re-checked per door.
    """
    if isinstance(candidate, NormalizedUrl):
        # Re-run the policy predicate: a value admitted under one posture can
        # cross a stricter door, and deny-list accounting must not be bypassed.
        if host_policy_reason(candidate.host, policy=policy) or host_egress_reason(candidate.host):
            return None
        return candidate
    if not candidate or not candidate.strip():
        return None
    try:
        return NormalizedUrl(candidate.strip(), policy=policy)
    except ValueError:
        return None


def admit_with_reason(candidate: str, *, policy: HostPolicy) -> tuple[NormalizedUrl | None, str | None]:
    """Admit ``candidate`` and surface the constructor's refusal reason as data.

    One wrapper over the one constructor, so a pre-flight door cannot drift from
    the checks admission itself performs; the reason is the ValueError text.
    """
    if not candidate or not candidate.strip():
        return None, "invalid_url"
    try:
        return NormalizedUrl(candidate.strip(), policy=policy), None
    except ValueError as error:
        return None, str(error)
