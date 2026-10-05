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

from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

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


def _canonical_parts(url: str, *, policy: HostPolicy) -> tuple[str, str, str, str, int | None, str, str]:
    """(canonical, scheme, host, site, port, path, query), or raise with the refusal reason."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("unsupported_scheme")
    if not parsed.hostname:
        raise ValueError("invalid_url")

    scheme = parsed.scheme
    host = parsed.hostname.lower().rstrip(".")
    reason = host_policy_reason(host, policy=policy) or host_egress_reason(host)
    if reason:
        raise ValueError(reason)

    default_port = 80 if scheme == "http" else 443
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
    canonical = urlunparse((scheme, netloc, path, parsed.params, query, ""))
    return canonical, scheme, host, registrable_domain(host), port, path, query


class NormalizedUrl:
    """An admitted URL: canonical, and carrying its host and its site.

    Immutable and compared by canonical string, so it can key a map or a cache
    the way the bare string used to. ``raw`` is the one explicit place a URL
    turns back into text.
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
        # The policy that admitted this value stays with it, so the object is
        # complete: anything it needs to re-justify itself is on the value,
        # not held by whichever caller happened to construct it. Containers
        # (visited / frontier / caches) are per-job and a job runs one policy,
        # so the carried policy is always the one that admitted each member.
        self._policy = policy

    @property
    def raw(self) -> str:
        """The canonical string: the wire form, the cache key, the spill field.
        This is also the value's durable identity -- equality, hashing and
        persistence all key on it, so two ``NormalizedUrl``s with the same
        canonical string are the same value."""
        return self._raw

    @property
    def scheme(self) -> str:
        return self._scheme

    @property
    def origin(self) -> str:
        """``scheme://host[:port]`` -- the fetch origin for robots/sitemaps."""
        default_port = 80 if self._scheme == "http" else 443
        if self._port is None or self._port == default_port:
            return f"{self._scheme}://{self._host}"
        return f"{self._scheme}://{self._host}:{self._port}"

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

        Every ``urljoin(base, href)`` + egress-filter pair in the tree is
        this: resolve, then admit with the same policy. Returns None when the
        reference does not yield a public page URL.
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
    def __get_pydantic_core_schema__(cls, source_type, handler):
        """Accept exactly one thing -- an already-admitted ``NormalizedUrl`` --
        and render as the canonical string on the wire.

        A raw string passed to a response model would have to admit itself,
        and a wire model has no ``HostPolicy``; refusing it keeps admission at
        the constructor, which is the one place a policy exists.
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

        A plain-validator core schema carries no JSON Schema the generator
        could infer, and the serializer is the truth this must match: the
        value crosses the wire as one string. Declaring it by hand keeps the
        OpenAPI document servable.
        """
        return {"type": "string"}


__all__ = ["NormalizedUrl", "admit_or_none"]


def admit_or_none(candidate: str | NormalizedUrl | None, *, policy: HostPolicy) -> NormalizedUrl | None:
    """Admit ``candidate`` as a public page URL, or drop it.

    The ``NormalizedUrl`` constructor is the enforcement chokepoint; this is
    the one None-returning wrapper for foreign candidates (search results,
    sitemap entries, robots hints) that every egress door shares, so a
    rejection is never one caller's hand-rolled ``try/except ValueError``.

    An already-admitted value is its own answer: it returns unchanged (the
    policy stored in it is the one it was admitted under), so callers handing
    values from one boundary to another cannot crash on text-only helpers.
    """
    if isinstance(candidate, NormalizedUrl):
        return candidate
    if not candidate or not candidate.strip():
        return None
    try:
        return NormalizedUrl(candidate.strip(), policy=policy)
    except ValueError:
        return None
