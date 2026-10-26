"""The canonical form of a URL that has crossed admission: constructing one IS
admission -- scheme/host validated against policy and the egress guard, with no
other way to build it. Host and site ride the value; ``raw`` is the text form.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

from app.models.host_policy import HostPolicy, host_egress_reason, host_policy_reason
from app.models.registrable_domain import registrable_domain

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


def _strip_trailing_colon(value: str) -> str:
    return value[:-1] if value.endswith(":") else value


def _bracketed_netloc(host: str, port: int | None) -> str:
    """Netloc with IPv6 literals re-bracketed; bare colons parse as host:port pairs."""
    host_part = f"[{host}]" if ":" in host else host
    return host_part if port is None else f"{host_part}:{port}"


def _canonical_parts(url: str, *, policy: HostPolicy) -> tuple[str, str, str, str, int | None, str, str]:
    """(canonical, scheme, host, site, port, path, query), or raise with the refusal reason."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")
    if parsed.username or parsed.password:
        # Userinfo is not a crawlable identity: credentials belong in
        # policy.credentials (origin-scoped), never in the URL itself.
        raise ValueError("userinfo_refused")

    scheme = parsed.scheme
    host = parsed.hostname.lower().rstrip(".")
    reason = host_policy_reason(host, policy=policy) or host_egress_reason(host)
    if reason:
        raise ValueError(reason)

    default_port = 80 if scheme == "http" else 443
    port = parsed.port
    if port == default_port:
        port = None
    netloc = _bracketed_netloc(host, port)
    # Path and query are identity: a trailing colon or slash in them can name a
    # distinct resource (#520), so only the authority segment is normalized here.
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
        canonical, scheme, host, site, port, path, query = _canonical_parts(raw, policy=policy)
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


__all__ = ["NormalizedUrl", "admit_or_none"]


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
